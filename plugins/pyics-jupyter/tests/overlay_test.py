"""
Live results in the TUI (``pyics_jupyter.overlay``), through a real ``Task`` with the
overlay registered on pyics's plugin manager: the results of the notebook's kernel are
merged per test with those of the saved notebook, labelled "(live)", explained by a
line below the summary, and never count as progress. The kernel is played by a
``KernelChannel`` writing hand-made results, on a fake clock.
"""

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from live_helpers import SOLUTION, TEMPLATE, FakeClock, text, write_notebook

from pyics import plugins
from pyics.data import Task
from pyics.notebook import cells
from pyics.tests.results import TestResult
from pyics.tests.tests import PrintTest, SimpleTest
from pyics_jupyter.channel import KernelChannel
from pyics_jupyter.overlay import NO_KERNEL, SOLVED_LIVE, LiveOverlay


class ClockedPlugin:
    """The plugin of pyics-jupyter, with overlays on the fake ``clock``."""

    def __init__(self, clock: FakeClock):
        self.clock = clock

    @plugins.hookimpl
    def pyics_results_overlay(self, task: Task) -> LiveOverlay | None:
        notebooks = [f for f in task.exercise_files if cells.is_notebook(f)]
        return LiveOverlay(task.path, notebooks, clock=self.clock, interval=0)


@pytest.fixture
def clock() -> FakeClock:
    """The time of the kernel and of the overlay, with the plugin registered."""
    clock = FakeClock()
    manager = plugins.manager()
    manager.unregister(name="jupyter")
    manager.register(ClockedPlugin(clock))
    return clock


def _save(path: Path, cells: list, at: float) -> None:
    """Save a notebook of ``cells`` to ``path``, as Jupyter would at the time ``at``."""
    write_notebook(path, cells)
    os.utime(path, (at, at))


@pytest.fixture
def notebook(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The exercise notebook, saved a minute ago with the template: adds and adds
    negatives fail, prints passes."""
    # where the pytest runs of the task write their cache
    monkeypatch.chdir(tmp_path)
    _save(tmp_path / "add.ipynb", TEMPLATE, clock() - 60)
    return tmp_path / "add.ipynb"


@pytest.fixture
def task_file(notebook: Path) -> Path:
    tests = [
        SimpleTest("adds", notebook, "add", 3, function_args=[1, 2]),
        SimpleTest("adds negatives", notebook, "add", -3, function_args=[-1, -2]),
        PrintTest("prints", notebook, "Adding\n"),
    ]
    return Task.create_task_json(
        notebook.parent / ".tasks/add", "Add", "", notebook, None, tests
    )


@pytest.fixture
def task(task_file: Path) -> Task:
    """The task, with the results of its tests on the saved notebook."""
    task = Task(task_file, setup=False)
    task.check_solved()
    return task


@pytest.fixture
def kernel(notebook: Path, clock: FakeClock) -> Iterator[Callable[[], KernelChannel]]:
    """Starts kernels for ``notebook``, without a heartbeat thread."""
    started: list[KernelChannel] = []

    def start() -> KernelChannel:
        started.append(KernelChannel(notebook, clock=clock))
        started[-1].open(heartbeat=False)
        return started[-1]

    yield start
    for k in started:
        k.close()


def _phases(name: str, index: int, outcome: str, message: str | None = None):
    """The results of a test's phases, as a kernel writes them."""
    return [
        TestResult(f"::{name}", "setup", "passed", 0.0, index=index),
        TestResult(f"::{name}", "call", outcome, 0.01, message, index=index),
        TestResult(f"::{name}", "teardown", "passed", 0.0, index=index),
    ]


def live_run(adds: str = "passed", negatives: str = "passed") -> list[TestResult]:
    """The results of a live run, whose print test is skipped, as in a kernel."""
    fails = {
        "adds": "adds failed: 0 == 3",
        "negatives": "adds negatives failed: 0 == -3",
    }
    return [
        *_phases("adds", 0, adds, None if adds == "passed" else fails["adds"]),
        *_phases(
            "adds negatives",
            1,
            negatives,
            None if negatives == "passed" else fails["negatives"],
        ),
        *_phases("prints", 2, "skipped", "Skipped: runs when the notebook is saved"),
    ]


def _at(clock: FakeClock) -> str:
    return time.strftime("%H:%M:%S", time.localtime(clock()))


def test_live_results_are_merged_per_test_and_labelled(
    task: Task, task_file: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    assert task.check_overlay()
    assert NO_KERNEL in text(task.status)
    assert "2 failed, 1 passed" in text(task.status)

    k = kernel()
    assert task.check_overlay()
    status = text(task.status)
    assert NO_KERNEL not in status and "Live results" not in status

    clock.advance(1)
    k.write({task_file: live_run(negatives="failed")}, 2)
    task.clear_test_results()
    assert task.check_overlay() and task.new_test_results
    status = text(task.status)
    assert (
        f"Live results from your notebook kernel (cell 2, {_at(clock)}). Save the "
        "notebook to count your progress." in status
    )
    # the print test keeps its saved result
    assert "1 failed, 2 passed" in status
    assert "FAILED ::adds negatives (live) - adds negatives failed: 0 == -3" in status
    assert SOLVED_LIVE not in status and not task.solved
    # nothing new: nothing to draw again
    assert not task.check_overlay()


def test_tests_without_a_live_result_keep_the_saved_one(
    task: Task, task_file: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    # of another version of the task
    k.write({task_file: _phases("subtracts", 0, "passed")}, None)
    task.check_overlay()
    status = text(task.status)
    assert "Live results" not in status and "(live)" not in status

    # interrupted while adds ran: the tests after it have no result
    interrupted = "adds failed: the kernel was interrupted while it ran"
    clock.advance(1)
    k.write({task_file: _phases("adds", 0, "failed", interrupted)}, None)
    task.check_overlay()
    status = text(task.status)
    assert f"Live results from your notebook kernel ({_at(clock)})." in status
    assert f"FAILED ::adds (live) - {interrupted}" in status
    assert "FAILED ::adds negatives - adds negatives failed: 0 == -3" in status


def test_progress_only_counts_the_saved_notebook(
    task: Task,
    task_file: Path,
    notebook: Path,
    kernel: Callable[[], KernelChannel],
    clock: FakeClock,
) -> None:
    k = kernel()
    clock.advance(1)
    k.write({task_file: live_run()}, 2)
    task.check_overlay()
    assert "3 passed" in text(task.status) and SOLVED_LIVE in text(task.status)
    assert not task.solved

    # saving it runs the tests on the saved notebook, which is newer
    clock.advance(1)
    _save(notebook, SOLUTION, clock())
    assert task.check_solved()
    status = text(task.status)
    assert "3 passed" in status
    assert "Live results" not in status and SOLVED_LIVE not in status

    # the next cell run is newer again, but doesn't undo the progress
    clock.advance(1)
    k.write({task_file: live_run(adds="failed")}, 2)
    assert task.check_overlay()
    assert "FAILED ::adds (live) - adds failed: 0 == 3" in text(task.status)
    assert task.solved


def test_a_kernel_that_is_killed_or_shuts_down_is_gone(
    task: Task, task_file: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    k.write({task_file: live_run()}, 2)
    task.check_overlay()
    assert "(live)" not in text(task.status)  # all pass
    assert "Live results" in text(task.status)

    # killed: no heartbeat for over 10 s
    clock.advance(11)
    assert task.check_overlay()
    status = text(task.status)
    assert NO_KERNEL in status and "Live results" not in status
    assert "2 failed, 1 passed" in status

    other = kernel()
    clock.advance(1)
    other.write({task_file: live_run()}, 2)
    assert task.check_overlay() and "Live results" in text(task.status)
    other.close()
    assert task.check_overlay() and NO_KERNEL in text(task.status)


def test_the_entry_point_gives_tasks_with_notebooks_an_overlay(
    task_file: Path, tmp_path: Path
) -> None:
    plugins.manager.cache_clear()
    hook = plugins.manager().hook.pyics_results_overlay
    assert isinstance(hook(task=Task(task_file, setup=False)), LiveOverlay)

    exercise = tmp_path / "add.py"
    exercise.write_text("def add(a, b):\n    return 0\n")
    test = SimpleTest("adds", exercise, "add", 3, function_args=[1, 2])
    python_task = Task.create_task_json(
        tmp_path / "py", "Add", "", exercise, None, test
    )
    assert hook(task=Task(python_task, setup=False)) is None
