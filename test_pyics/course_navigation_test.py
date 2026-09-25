"""
Navigate a ``Course`` with ``next``/``previous``, and check that ``Task.create_files`` keeps
the student's exercise files when a task is revisited.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from pyics.data import Course, Task

A, B, C = (Path(f"{n}.pyicstask.json") for n in "abc")


class _FakeTask:
    """Stands in for ``Task`` so no files are written and no tests are run."""

    solved_paths: ClassVar[set[Path]] = set()

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def solved(self) -> bool:
        return self.path in self.solved_paths


@pytest.fixture
def course(monkeypatch: pytest.MonkeyPatch) -> Course:
    monkeypatch.setattr("pyics.data.course.Task", _FakeTask)
    monkeypatch.setattr(_FakeTask, "solved_paths", set())
    c = Course(name="c", tasks={"a": A, "b": B, "c": C}, progress=[0, 0, 0])
    c.clear_task_changed()  # set by __init__ for the initial draw
    return c


# --- next ---


def test_next_on_solved_task_advances(course: Course) -> None:
    _FakeTask.solved_paths.add(A)
    assert course.next().path == B
    assert course.task_changed


def test_next_on_unsolved_task_stays(course: Course) -> None:
    assert course.next().path == A
    assert not course.task_changed


def test_next_on_last_task_stays(course: Course) -> None:
    _FakeTask.solved_paths.update({A, B, C})
    course.next()
    assert course.next().path == C
    course.clear_task_changed()

    assert course.next().path == C
    assert not course.task_changed


# --- previous ---


def test_previous_on_first_task_stays(course: Course) -> None:
    assert course.previous().path == A
    assert not course.task_changed


def test_previous_goes_back(course: Course) -> None:
    _FakeTask.solved_paths.add(A)
    course.next()
    course.clear_task_changed()

    assert course.previous().path == A
    assert course.task_changed


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
