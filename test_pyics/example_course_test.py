"""
Test the example course ``examples/search_notebook``: the DFS and BFS tasks of the
search course, sharing one notebook with a section per task.
"""

import json
import shutil
import time
from pathlib import Path

import pytest

from pyics.data import Course, Task
from pyics.notebook import cells, sections

EXAMPLE = Path(__file__).parents[1] / "examples/search_notebook"


@pytest.fixture
def course_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the example course, without the files a run of it creates, in the
    working directory."""
    root = tmp_path / "search_notebook"
    shutil.copytree(
        EXAMPLE, root, ignore=shutil.ignore_patterns(".solutions", "*.ipynb", ".pyics")
    )
    monkeypatch.chdir(tmp_path)
    return root


def _write_stored_files(root: Path) -> tuple[Path, Path]:
    """Write the exercise and solution notebooks the tasks store. Returns their
    paths."""
    stored = Task.read_task_json(root / ".tasks/dfs.pyicstask.json")["file_contents"]
    exercise = root / "search_algorithms.ipynb"
    solution = root / ".solutions/search_algorithms.ipynb"
    for path in (exercise, solution):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stored[path], encoding="utf-8")
    return exercise, solution


def _solved(root: Path) -> dict[str, bool]:
    return {
        key: Task(root / f".tasks/{key}.pyicstask.json", setup=False).check_solved()
        for key in ("dfs", "bfs")
    }


def _code_cell(nb: dict, source_start: str) -> dict:
    return next(
        cell
        for cell in nb["cells"]
        if cell["cell_type"] == "code"
        and "".join(cell["source"]).startswith(source_start)
    )


def test_both_tasks_store_the_same_notebooks(course_dir: Path) -> None:
    dfs = Task.read_task_json(course_dir / ".tasks/dfs.pyicstask.json")
    bfs = Task.read_task_json(course_dir / ".tasks/bfs.pyicstask.json")

    assert (
        dfs["ex_files"] == bfs["ex_files"] == [course_dir / "search_algorithms.ipynb"]
    )
    assert dfs["file_contents"] == bfs["file_contents"]
    assert dfs["sections"] == {dfs["ex_files"][0]: "dfs"}
    assert bfs["sections"] == {bfs["ex_files"][0]: "bfs"}
    # stored normalized, i.e. as ``create_task_json`` stores them
    for text in dfs["file_contents"].values():
        assert cells.normalize(text) == text


def test_sections_are_the_exercises(course_dir: Path) -> None:
    exercise, _ = _write_stored_files(course_dir)

    found = sections.find_sections(cells.read(exercise))

    assert list(found) == ["dfs", "bfs"]
    assert found["dfs"].title == "Exercise 1: Depth-First Search"
    assert found["bfs"].title == "Exercise 2: Breadth-First Search"


def test_the_exercise_is_unsolved_and_the_solution_solves_both_tasks(
    course_dir: Path,
) -> None:
    exercise, solution = _write_stored_files(course_dir)

    assert _solved(course_dir) == {"dfs": False, "bfs": False}

    shutil.copy(solution, exercise)
    assert _solved(course_dir) == {"dfs": True, "bfs": True}


def test_tasks_progress_independently(course_dir: Path) -> None:
    exercise, solution = _write_stored_files(course_dir)
    nb, solved_nb = json.loads(exercise.read_text()), json.loads(solution.read_text())
    _code_cell(nb, "# TODO: implement the depth")["source"] = _code_cell(
        solved_nb, "def depth_first_search"
    )["source"]
    exercise.write_text(json.dumps(nb), encoding="utf-8")

    assert _solved(course_dir) == {"dfs": True, "bfs": False}


def test_recursion_only_fails_the_task_of_its_section(course_dir: Path) -> None:
    exercise, solution = _write_stored_files(course_dir)
    nb = json.loads(solution.read_text())
    bfs = _code_cell(nb, "def breadth_first_search")
    bfs["source"] = "".join(bfs["source"]) + (
        "\n\n\ndef countdown(n):\n    return countdown(n - 1) if n else 0\n"
    )
    exercise.write_text(json.dumps(nb), encoding="utf-8")

    assert _solved(course_dir) == {"dfs": True, "bfs": False}


def test_the_course_starts_on_the_first_unsolved_task(course_dir: Path) -> None:
    exercise, solution = _write_stored_files(course_dir)
    nb, solved_nb = json.loads(exercise.read_text()), json.loads(solution.read_text())
    _code_cell(nb, "# TODO: implement the depth")["source"] = _code_cell(
        solved_nb, "def depth_first_search"
    )["source"]
    exercise.write_text(json.dumps(nb), encoding="utf-8")

    course = Course.load_course_manifest(
        course_dir / "search_algorithms.pyicscourse.json", check_progress=True
    )
    deadline = time.monotonic() + 30
    while course.loading or course.check_status:
        assert time.monotonic() < deadline, "the progress check didn't finish"
        time.sleep(0.01)

    assert course.name == "Search Algorithms (Notebook)"
    assert course.instructions.startswith("Implement breadth-first search")
    assert course.progress == 0.5
    # the student's work is kept
    assert json.loads(exercise.read_text()) == nb
