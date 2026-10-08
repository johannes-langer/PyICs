"""
Navigate a ``Course`` with ``next``/``previous``, check a course's progress from the
exercise files, and check that ``Task.create_files`` keeps the student's exercise files
when a task is revisited.
"""

import threading
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import ClassVar

import pytest

from pyics.data import Course, Task
from pyics.tests.tests import SimpleTest
from pyics.util.paths import StrPath

A, B, C = (Path(f"{n}.pyicstask.json") for n in "abc")


class _FakeTask:
    """Stands in for ``Task`` so no files are written and no tests are run."""

    solved_paths: ClassVar[set[Path]] = set()
    # the paths of the tasks checked by ``check_solved``, in order
    checked: ClassVar[list[Path]] = []
    # called by ``check_solved`` before it returns, e.g. to block
    on_check: ClassVar[staticmethod[[Path], object]] = staticmethod(lambda path: None)

    def __init__(self, path: Path, *, setup: bool = True) -> None:
        self.path = path
        self.is_set_up = setup

    def setup(self) -> None:
        self.is_set_up = True

    def check_solved(self) -> bool:
        self.checked.append(self.path)
        self.on_check(self.path)
        return self.solved

    @property
    def solved(self) -> bool:
        return self.path in self.solved_paths


@pytest.fixture(autouse=True)
def fake_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FakeTask, "solved_paths", set())
    monkeypatch.setattr(_FakeTask, "checked", [])
    monkeypatch.setattr(_FakeTask, "on_check", staticmethod(lambda path: None))


@pytest.fixture
def course(monkeypatch: pytest.MonkeyPatch) -> Course:
    monkeypatch.setattr("pyics.data.course.Task", _FakeTask)
    c = Course(name="c", tasks={"a": A, "b": B, "c": C}, progress=[0, 0, 0])
    c.clear_task_changed()  # set by __init__ for the initial draw
    return c


def _path(task: Task | None) -> StrPath:
    """The path of ``task``, which is only None while the course is loading."""
    assert task is not None
    return task.path


# --- next ---


def test_next_on_solved_task_advances(course: Course) -> None:
    _FakeTask.solved_paths.add(A)
    assert _path(course.next()) == B
    assert course.task_changed


def test_next_on_unsolved_task_stays(course: Course) -> None:
    assert _path(course.next()) == A
    assert not course.task_changed


def test_next_on_last_task_stays(course: Course) -> None:
    _FakeTask.solved_paths.update({A, B, C})
    course.next()
    assert _path(course.next()) == C
    course.clear_task_changed()

    assert _path(course.next()) == C
    assert not course.task_changed


# --- previous ---


def test_previous_on_first_task_stays(course: Course) -> None:
    assert _path(course.previous()) == A
    assert not course.task_changed


def test_previous_goes_back(course: Course) -> None:
    _FakeTask.solved_paths.add(A)
    course.next()
    course.clear_task_changed()

    assert _path(course.previous()) == A
    assert course.task_changed


# --- starting ---


def test_starts_on_the_first_unsolved_task_of_the_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pyics.data.course.Task", _FakeTask)
    c = Course(name="c", tasks={"a": A, "b": B, "c": C}, progress=[1, 0, 0])
    assert _path(c.next()) == B
    assert c.task_changed


def test_a_solved_course_starts_on_its_last_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyics.data.course.Task", _FakeTask)
    c = Course(name="c", tasks={"a": A, "b": B, "c": C}, progress=[1, 1, 1])
    assert _path(c.previous()) == B  # i.e. it was on C


# --- checking the progress ---


def _checked_course(progress: list[int]) -> Course:
    return Course(
        name="c", tasks={"a": A, "b": B, "c": C}, progress=progress, check_progress=True
    )


def _wait_for_check(course: Course) -> None:
    deadline = time.monotonic() + 5
    while course.check_status:
        assert time.monotonic() < deadline, "the progress check didn't finish"
        time.sleep(0.01)


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[int]], Course]:
    """Makes a course of three tasks that checks its progress from the (fake) tasks."""
    monkeypatch.setattr("pyics.data.course.Task", _FakeTask)
    return _checked_course


def test_check_starts_on_the_first_unsolved_task(
    checked: Callable[[list[int]], Course],
) -> None:
    _FakeTask.solved_paths.update({A, C})
    course = checked([0, 1, 0])  # the manifest's progress is out of date
    _wait_for_check(course)

    assert not course.loading
    assert course.task_changed
    current = course.next()  # unsolved, so it stays
    assert isinstance(current, _FakeTask) and current.path == B
    assert current.is_set_up
    assert course.progress == Fraction(2, 3)
    # every task is checked once, including the one started on
    assert _FakeTask.checked == [A, B, C]


def test_check_of_a_solved_course_starts_on_its_last_task(
    checked: Callable[[list[int]], Course],
) -> None:
    _FakeTask.solved_paths.update({A, B, C})
    course = checked([0, 0, 0])
    _wait_for_check(course)
    assert _path(course.previous()) == B  # i.e. it was on C
    assert course.progress == 1


def test_course_is_loading_until_a_task_to_start_on_is_found(
    checked: Callable[[list[int]], Course],
) -> None:
    _FakeTask.solved_paths.add(A)
    release = threading.Event()
    _FakeTask.on_check = staticmethod(lambda path: release.wait(5))
    course = checked([0, 0, 0])

    assert course.loading
    assert course.check_status == "Checking your progress... (task 1/3)"
    assert course.next() is None and course.previous() is None
    assert course.instructions == "" and course.exercise_files == []
    release.set()
    _wait_for_check(course)
    assert not course.loading


def test_check_can_be_stopped_between_tasks(
    checked: Callable[[list[int]], Course],
) -> None:
    _FakeTask.solved_paths.update({A, B})
    checking = threading.Event()
    release = threading.Event()

    def block(path: Path) -> None:
        checking.set()
        release.wait(5)

    _FakeTask.on_check = staticmethod(block)
    course = checked([0, 0, 0])
    assert checking.wait(5)
    threading.Timer(0.1, release.set).start()
    course.stop_progress_check(timeout=5)

    assert _FakeTask.checked == [A]
    assert course.check_status == ""
    assert course.loading


def test_a_task_that_cant_be_loaded_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checked: Callable[[list[int]], Course],
) -> None:
    monkeypatch.chdir(tmp_path)  # where the warning is logged to
    def broken(path: Path, *, setup: bool = True) -> _FakeTask:
        raise FileNotFoundError(path)

    monkeypatch.setattr("pyics.data.course.Task", broken)
    course = checked([0, 0, 0])
    _wait_for_check(course)
    assert isinstance(course.error, FileNotFoundError)
    assert course.loading


def test_checking_a_task_without_its_exercise_file_creates_no_files(
    tmp_path: Path,
) -> None:
    ex, sol = tmp_path / "add.py", tmp_path / "add_solution.py"
    ex.write_text("def add(a, b):\n    return 0\n")
    sol.write_text("def add(a, b):\n    return a + b\n")
    test = SimpleTest("adds", ex, "add", 3, function_args=[1, 2])
    task_file = Task.create_task_json(tmp_path / "add", "add", "", ex, sol, [test])
    ex.unlink()
    sol.unlink()

    task = Task(task_file, setup=False)
    assert not task.check_solved()
    assert not ex.exists() and not sol.exists()

    threads = set(threading.enumerate())
    task.setup()  # runs the tests, as they haven't been run
    assert ex.read_text() == "def add(a, b):\n    return 0\n"
    assert sol.exists()
    # a pytest run changes back to the directory it started in when it finishes, which
    # mustn't happen during another test
    for thread in set(threading.enumerate()) - threads:
        thread.join(5)
    assert task.test_results and not task.solved


# --- task files ---


def test_create_files_keeps_existing_exercise_files(tmp_path: Path) -> None:
    task = Task.__new__(Task)  # skip __init__, which would run the task's tests
    new_ex, old_ex, sol = tmp_path / "new.py", tmp_path / "old.py", tmp_path / "sol.py"
    old_ex.write_text("student work")
    sol.write_text("stale solution")
    task.exercise_files = [new_ex, old_ex]
    task.solution_files = [sol]

    task.create_files(["template", "template"], ["solution"])
    assert new_ex.read_text() == "template"
    assert old_ex.read_text() == "student work"
    assert sol.read_text() == "solution"

    task.create_files(["template", "template"], ["solution"], overwrite=True)
    assert old_ex.read_text() == "template"
