"""
Tests run the students' code in the directory of the exercise file, as they run it, so
that it finds the files next to it by relative paths.
"""

from pathlib import Path

import pytest
from notebook_helpers import code, write_notebook

from pyics.scripts.create_task import _load_namespace
from pyics.tests import GeneralTest, PrintTest, SimpleTest, SpecTest
from pyics.tests.pytest_integration import run_tests

EXERCISE = """
from pathlib import Path

# read as the module is loaded, and again when called
EDGES = Path("edges.csv").read_text().split()

def count_edges() -> int:
    return len(Path("edges.csv").read_text().split())

if __name__ == "__main__":
    print(count_edges())
"""

GENERAL = """
def test_count(solution):
    assert solution.count_edges() == 2
"""


@pytest.fixture
def exercise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An exercise file that reads a data file next to it, while the tests run
    somewhere else."""
    course = tmp_path / "course"
    course.mkdir()
    (course / "edges.csv").write_text("A,B\nB,C\n")
    path = course / "exercise.py"
    path.write_text(EXERCISE)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return path


def test_tests_run_in_the_exercise_files_directory(exercise: Path) -> None:
    relative = Path("..", "course", "exercise.py")
    for path in (exercise, relative):
        simple = SimpleTest("t", path, "count_edges", 2)
        simple.run()
        assert simple.file_path == path  # restored
        PrintTest("t", path, "2").run()
        SpecTest("t", path, "count_edges", {"is_class": False, "return": "int"}).run()
    assert Path.cwd().name == "work"  # restored


def test_general_tests_run_in_the_exercise_files_directory(exercise: Path) -> None:
    results = run_tests([GeneralTest("general", exercise, GENERAL)])
    assert [(r.nodeid, r.outcome) for r in results if r.when == "call"] == [
        ("::general::test_count", "passed")
    ]
    assert Path.cwd().name == "work"


def test_notebooks_run_in_their_directory(exercise: Path) -> None:
    notebook = write_notebook(
        exercise.with_suffix(".ipynb"),
        [code(EXERCISE.replace('if __name__ == "__main__":\n    ', ""))],
    )
    SimpleTest("t", notebook, "count_edges", 2).run()
    PrintTest("t", notebook, "2").run()


def test_isolated_tests_still_run_in_a_directory_of_their_own(exercise: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SimpleTest("t", exercise, "count_edges", 2, isolated=True).run()


def test_a_missing_exercise_file_fails_as_before(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SimpleTest("t", tmp_path / "missing" / "exercise.py", "f", 1).run()


def test_the_info_file_runs_in_its_directory(exercise: Path) -> None:
    info = exercise.with_name("info.py")
    info.write_text(EXERCISE.replace("__main__", "never"))
    symbols, problem = _load_namespace(str(info))
    assert (symbols["EDGES"], problem) == (["A,B", "B,C"], "")
    assert Path.cwd().name == "work"
