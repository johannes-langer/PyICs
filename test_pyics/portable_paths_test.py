"""
Task and course files store paths relative to their own directory, so a course keeps
working when it is moved, and regardless of the working directory pyics is run from.
"""

import shutil
from pathlib import Path

import pytest

import pyics.util.json_handling as json
from pyics.data import Course, Task
from pyics.tests.tests import PrintTest
from pyics.util import paths


@pytest.fixture(autouse=True)
def no_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Task.__init__`` would run the task's tests with pytest."""
    monkeypatch.setattr(Task, "run_tests", lambda self: None)


def _create_course(root: Path) -> Path:
    """Write a course like the demo course, from within ``root``: tasks, exercises and
    solutions in sibling directories. Returns the manifest path, relative to ``root``."""
    (root / ".exercises").mkdir()
    (root / ".solutions").mkdir()
    (root / ".exercises/a.py").write_text("print('todo')")
    (root / ".solutions/a.py").write_text("print('done')")

    task_fp = Task.create_task_json(
        Path(".tasks/a"),
        "A",
        "Do it",
        Path(".exercises/a.py"),
        Path(".solutions/a.py").absolute(),  # absolute input is stored relative, too
        PrintTest("prints", Path(".exercises/a.py"), "done\n"),
    )
    return Course.create_course_manifest(Path(".courses/c"), [task_fp])


def test_relative_to_and_resolve_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "ex" / "a.py"
    rel = paths.relative_to(target, tmp_path / "tasks")
    assert rel == Path("../ex/a.py")
    assert paths.resolve(rel, tmp_path / "tasks") == target


def test_resolve_keeps_absolute_paths(tmp_path: Path) -> None:
    assert paths.resolve(tmp_path / "a.py", "elsewhere") == tmp_path / "a.py"


def test_files_store_relative_posix_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    course_fp = _create_course(tmp_path)

    task = json.loads((tmp_path / ".tasks/a.pyicstask.json").read_text())
    assert task["ex_files"] == [Path("../.exercises/a.py")]
    assert task["sol_files"] == [Path("../.solutions/a.py")]
    assert task["tests"][0].file_path == Path("../.exercises/a.py")

    course = json.loads(course_fp.read_text())
    assert course["tasks"] == {"A": Path("../.tasks/a.pyicstask.json")}

    for text in (tmp_path / ".tasks/a.pyicstask.json").read_text(), course_fp.read_text():
        assert str(tmp_path) not in text
        assert "\\\\" not in text  # forward slashes on all platforms


def test_moved_course_loads_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, moved = tmp_path / "original", tmp_path / "moved"
    original.mkdir()
    monkeypatch.chdir(original)
    course_fp = _create_course(original)

    monkeypatch.chdir(tmp_path)
    shutil.move(original, moved)
    shutil.rmtree(moved / ".exercises")  # recreated from the task file
    monkeypatch.chdir(moved / ".tasks")  # neither the old nor the course root

    course = Course.load_course_manifest(moved / course_fp)
    assert course.exercise_files == [moved / ".exercises/a.py"]
    assert (moved / ".exercises/a.py").read_text() == "print('todo')"

    task = Task(moved / ".tasks/a.pyicstask.json")
    assert task.solution_files == [moved / ".solutions/a.py"]
    assert task.tests[0].file_path == moved / ".exercises/a.py"


def test_caller_tests_keep_their_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").touch()
    test = PrintTest("prints", tmp_path / "a.py", "")
    Task.create_task_json(Path("sub/a"), "A", "", "a.py", None, test)
    assert test.file_path == tmp_path / "a.py"


def test_absolute_paths_in_old_files_still_load(tmp_path: Path) -> None:
    ex = tmp_path / "ex" / "a.py"
    task_fp = tmp_path / "tasks" / "a.pyicstask.json"
    task_fp.parent.mkdir()
    task_fp.write_text(
        json.dumps({
            "name": "A",
            "ex_files": [ex],
            "sol_files": [],
            "exercise_code": ["x = 1"],
            "solution_code": [],
            "tests": [PrintTest("prints", ex, "")],
        })
    )

    task = Task(task_fp)
    assert task.exercise_files == [ex]
    assert task.tests[0].file_path == ex


def test_create_files_creates_missing_directories(tmp_path: Path) -> None:
    task = Task.__new__(Task)  # skip __init__
    task.exercise_files = [tmp_path / "ex" / "a.py"]
    task.solution_files = [tmp_path / "deep" / "sol" / "a.py"]

    task.create_files(["template"], ["solution"])
    assert task.exercise_files[0].read_text() == "template"
    assert task.solution_files[0].read_text() == "solution"
