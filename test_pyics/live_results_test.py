"""
Live results in the TUI: a task with a notebook exercise file shows the results that
the notebook's kernel writes to its channel (``Task.check_live_results``), merged per
test with those of the saved notebook, says where they come from, and never counts
them as progress. The kernel is played by a ``KernelChannel`` writing hand-made
``TestResult``s, on a fake clock.
"""

import io
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from fractions import Fraction
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from notebook_helpers import markdown, write_notebook
from rich.console import Console

from pyics.app.cli import CLI
from pyics.app.pyics import PyIC
from pyics.data import Course, Task
from pyics.data.course import KERNEL_HINT
from pyics.data.task import SOLVED_LIVE
from pyics.notebook.channel import (
    PROTOCOL,
    CellRef,
    ChannelReader,
    KernelChannel,
    channel_dir,
    read_json,
)
from pyics.tests.results import TestResult
from pyics.tests.tests import PrintTest, SimpleTest

TIMEOUT = 10  # seconds

TEMPLATE = [markdown("# Adding"), "def add(a, b):\n    return 0\n", 'print("Adding")']
SOLUTION = [
    markdown("# Adding"),
    "def add(a, b):\n    return a + b\n",
    'print("Adding")',
]
CELL = CellRef("cell-2", 2)

NO_KERNEL = "No notebook kernel connected: results are from your saved notebook."
RESTARTED = "Your notebook kernel restarted: run your cells to see live results."
TIMED_OUT = (
    "No notebook kernel connected: your notebook kernel stopped responding, so "
    "results are from your saved notebook."
)
CONNECTED = "Your notebook kernel is connected: run a cell to see live results."
SAVED_LATER = (
    "Results from your saved notebook, which was saved after your last cell run"
)


class FakeClock:
    """Time that only passes when a test says so, for the kernel and the TUI. It starts
    at the real time, so that it agrees with the times of files it doesn't set."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def at(self) -> str:
        """The time now, as status lines show it."""
        return time.strftime("%H:%M:%S", time.localtime(self.now))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The time of the kernel and of the TUI's channel readers, which read the channel's
    files at every poll (as files written in quick succession may look unchanged)."""
    clock = FakeClock()
    monkeypatch.setattr(
        "pyics.data.task.ChannelReader",
        partial(ChannelReader, clock=clock, reread_interval=0.0),
    )
    return clock


@pytest.fixture
def chdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working directory, where pytest runs started by a test write their cache."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


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


def _save(path: Path, cells: list[Any], at: float) -> None:
    """Save a notebook of ``cells`` to ``path``, as Jupyter would at the time ``at``."""
    write_notebook(path, cells)
    os.utime(path, (at, at))


@pytest.fixture
def notebook(chdir: Path, clock: FakeClock) -> Path:
    """The exercise notebook, saved a minute ago with the template: adds and adds
    negatives fail, prints passes."""
    path = chdir / "add.ipynb"
    _save(path, TEMPLATE, clock.now - 60)
    return path


@pytest.fixture
def task_file(notebook: Path, chdir: Path) -> Path:
    solution = write_notebook(chdir / ".solutions/add.ipynb", SOLUTION)
    tests = [
        SimpleTest("adds", notebook, "add", 3, function_args=[1, 2]),
        SimpleTest("adds negatives", notebook, "add", -3, function_args=[-1, -2]),
        PrintTest("prints", notebook, "Adding\n"),
    ]
    return Task.create_task_json(
        chdir / ".tasks/add", "Add", "Implement add.", notebook, solution, tests
    )


@pytest.fixture
def manifest(task_file: Path, chdir: Path) -> Path:
    """A course of the notebook task, followed by a task with a Python file."""
    double = chdir / "double.py"
    double.write_text("def double(x):\n    return 0\n")
    other = Task.create_task_json(
        chdir / ".tasks/double",
        "Double",
        "Implement double.",
        double,
        tests=SimpleTest("doubles", double, "double", 4, function_args=[2]),
    )
    return Course.create_course_manifest(chdir / "course", [task_file, other])


@pytest.fixture
def task(task_file: Path) -> Task:
    """The task, with the results of its tests on the saved notebook."""
    task = Task(task_file, setup=False)
    task.check_solved()
    return task


@pytest.fixture
def kernel(
    notebook: Path, clock: FakeClock, task_file: Path
) -> Iterator[Callable[..., KernelChannel]]:
    """Starts a kernel for ``notebook``, without a heartbeat thread, with the given
    ``KernelChannel`` arguments. Its files are removed after the test."""
    started: list[KernelChannel] = []

    def start(**kwargs: Any) -> KernelChannel:
        kernel = KernelChannel(notebook, clock=clock, **kwargs)
        kernel.register([task_file], heartbeat=False)
        started.append(kernel)
        return kernel

    yield start
    for kernel in started:
        kernel.close()


def _phases(
    name: str,
    index: int,
    outcome: str,
    message: str | None = None,
    trace: str | None = None,
) -> list[TestResult]:
    """The results of a test's phases, as a kernel writes them."""
    nodeid = f"::{name}"
    return [
        TestResult(nodeid, "setup", "passed", 0.0, index=index),
        TestResult(nodeid, "call", outcome, 0.01, message, trace, index),
        TestResult(nodeid, "teardown", "passed", 0.0, index=index),
    ]


def live_run(
    adds: str = "passed", negatives: str = "passed", trace: str | None = None
) -> list[TestResult]:
    """The results of a live run: the PrintTest is skipped, as in a kernel."""
    return [
        *_phases(
            "adds",
            0,
            adds,
            None if adds == "passed" else "adds failed: 0 == 3",
            None if adds == "passed" else trace,
        ),
        *_phases(
            "adds negatives",
            1,
            negatives,
            None if negatives == "passed" else "adds negatives failed: 0 == -3",
        ),
        *_phases("prints", 2, "skipped", "Skipped: runs when the notebook is saved"),
    ]


def _status(shown: Task | Course) -> str:
    console = Console(width=300, record=True, file=io.StringIO())
    console.print(shown.status)
    return console.export_text()


def _calls(task: Task) -> dict[str, str]:
    """The outcome of each test, as shown."""
    results, _ = task.shown_results()
    return {r.nodeid.removeprefix("::"): r.outcome for r in results if r.when == "call"}


def _wait_for(condition: Callable[[], object]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# --- live results ---


def test_live_results_are_shown_within_a_tick_merged_per_test(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    assert _calls(task) == {
        "adds": "failed",
        "adds negatives": "failed",
        "prints": "passed",
    }
    assert task.check_live_results()
    assert NO_KERNEL in _status(task)

    k = kernel()
    assert task.check_live_results()
    assert CONNECTED in _status(task)

    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run(negatives="failed")})
    task.clear_test_results()
    assert task.check_live_results()
    assert task.new_test_results
    # each test shows its newest result, but the PrintTest's live one is skipped
    assert _calls(task) == {
        "adds": "passed",
        "adds negatives": "failed",
        "prints": "passed",
    }
    assert task.shown_results()[1] == {0, 1}
    status = _status(task)
    assert (
        f"Live results from your notebook kernel (cell 2, {clock.at()}). Save the "
        "notebook to count your progress." in status
    )
    # live failures are labelled as such
    assert "::adds negatives (live)" in status
    assert "FAILED ::adds negatives (live) - adds negatives failed: 0 == -3" in status
    assert "1 failed, 2 passed" in status
    # live results never count
    assert not task.solved

    # nothing new: nothing to draw again
    task.clear_test_results()
    assert not task.check_live_results()
    assert not task.new_test_results


def test_a_wrongly_redefined_function_turns_red(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run()})
    task.check_live_results()
    assert _calls(task) == {
        "adds": "passed",
        "adds negatives": "passed",
        "prints": "passed",
    }
    assert "3 passed" in _status(task)

    clock.advance(1)
    trace = "\x1b[31m  2 | def add(a, b):\x1b[0m\n"
    k.write_results(
        2, CellRef("cell-7", None), {task_file: live_run("failed", trace=trace)}
    )
    assert task.check_live_results()
    assert _calls(task)["adds"] == "failed"
    status = _status(task)
    assert "FAILED ::adds (live) - adds failed: 0 == 3" in status
    assert "2 | def add(a, b):" in status  # the kernel traced it
    # a cell that isn't in the saved notebook
    assert f"(an unsaved cell, {clock.at()})" in status


def test_tests_without_a_live_result_keep_their_saved_one(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    # interrupted while adds ran: the later tests have no result
    interrupted = _phases(
        "adds",
        0,
        "failed",
        "adds failed: the kernel was interrupted while it ran, e.g. in an endless loop.",
    )
    k.write_results(1, CELL, {task_file: interrupted})
    task.check_live_results()
    assert _calls(task) == {
        "adds": "failed",
        "adds negatives": "failed",
        "prints": "passed",
    }
    assert task.shown_results()[1] == {0}
    assert "the kernel was interrupted" in _status(task)


def test_a_skipped_live_print_test_keeps_its_saved_result(
    notebook: Path,
    task_file: Path,
    kernel: Callable[..., KernelChannel],
    clock: FakeClock,
) -> None:
    _save(notebook, SOLUTION, clock.now - 30)
    task = Task(task_file, setup=False)
    assert task.check_solved()

    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run(adds="failed")})
    task.check_live_results()
    assert _calls(task) == {
        "adds": "failed",
        "adds negatives": "passed",
        "prints": "passed",
    }
    # the saved notebook still counts as solved
    assert task.solved


def test_results_of_another_version_of_the_task_are_ignored(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    renamed = _phases("subtracts", 0, "passed") + _phases("adds negatives", 1, "passed")
    k.write_results(1, CELL, {task_file: renamed})
    task.check_live_results()
    assert _calls(task) == {
        "adds": "failed",
        "adds negatives": "passed",
        "prints": "passed",
    }


# --- progress ---


def test_progress_changes_only_after_a_save(
    manifest: Path,
    notebook: Path,
    task_file: Path,
    kernel: Callable[..., KernelChannel],
    clock: FakeClock,
    no_new_threads: None,
) -> None:
    course = Course.load_course_manifest(manifest)
    _wait_for(lambda: course.tick()[0] and "2 failed, 1 passed" in _status(course))
    assert NO_KERNEL in _status(course)

    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run()})
    ran_at = clock.at()
    new_results, new_progress = course.tick()
    assert new_results and not new_progress
    status = _status(course)
    assert "Live results from your notebook kernel" in status
    assert "3 passed" in status and SOLVED_LIVE in status
    assert course.progress == 0
    # the task isn't solved, so n stays on it
    course.clear_task_changed()
    task = course.next()
    assert task is not None and Path(task.path) == task_file
    assert not course.task_changed and not task.solved and task.solved_live

    # saving reruns the tests on the saved notebook, which are newer
    clock.advance(1)
    _save(notebook, SOLUTION, clock.now)
    _wait_for(lambda: (course.tick(), course.progress == Fraction(1, 2))[1])
    status = _status(course)
    assert f"{SAVED_LATER} (cell 2, {ran_at})." in status
    assert "3 passed" in status and SOLVED_LIVE not in status
    assert task.solved and not task.solved_live

    # the next cell run is newer again
    clock.advance(1)
    k.write_results(2, CELL, {task_file: live_run(negatives="failed")})
    course.tick()
    assert "Live results from your notebook kernel" in _status(course)
    assert "1 failed, 2 passed" in _status(course)
    # ... but doesn't undo the progress
    assert course.progress == Fraction(1, 2)


def test_a_tui_restart_shows_the_live_results_at_once(
    manifest: Path,
    task_file: Path,
    kernel: Callable[..., KernelChannel],
    clock: FakeClock,
    no_new_threads: None,
) -> None:
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run()})

    # the progress is checked from the saved notebook, as ever
    course = Course.load_course_manifest(manifest, check_progress=True)
    _wait_for(lambda: not course.loading and not course.check_status)
    assert course.progress == 0
    new_results, _ = course.tick()
    assert new_results
    status = _status(course)
    assert f"Live results from your notebook kernel (cell 2, {clock.at()})" in status
    assert SOLVED_LIVE in status
    assert course.progress == 0


# --- status lines ---


def test_tests_passing_live_but_not_saved_are_named(
    task: Task,
    notebook: Path,
    kernel: Callable[..., KernelChannel],
    task_file: Path,
    clock: FakeClock,
) -> None:
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run(negatives="failed")})
    task.check_live_results()
    assert (
        "adds passes in your kernel, but not in the saved notebook: was a cell "
        "deleted or not saved?" in _status(task)
    )

    clock.advance(1)
    k.write_results(2, CELL, {task_file: live_run()})
    task.check_live_results()
    assert "adds and adds negatives pass in your kernel, but not in the saved" in (
        _status(task)
    )

    # e.g. the cell defining add was deleted, and the notebook saved
    clock.advance(1)
    os.utime(notebook, (clock.now, clock.now))
    task.check_solved()
    status = _status(task)
    assert SAVED_LATER in status
    assert "2 failed, 1 passed" in status
    assert "adds and adds negatives pass in your kernel" in status


def test_a_restarted_kernel_drops_the_live_results(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    old = kernel(connection_file="/run/jupyter/kernel-1.json")
    clock.advance(1)
    old.write_results(1, CELL, {task_file: live_run()})
    task.check_live_results()
    assert "3 passed" in _status(task)

    clock.advance(1)
    new = kernel(connection_file="/run/jupyter/kernel-1.json")
    assert task.check_live_results()
    status = _status(task)
    assert RESTARTED in status and "2 failed, 1 passed" in status
    assert task.live_results == []

    clock.advance(1)
    new.write_results(1, CELL, {task_file: live_run(negatives="failed")})
    task.check_live_results()
    assert "Live results from your notebook kernel" in _status(task)
    assert _calls(task)["adds"] == "passed"


def test_a_kernel_without_heartbeat_is_gone(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run()})
    task.check_live_results()
    clock.advance(9)
    assert not task.check_live_results()
    assert "3 passed" in _status(task)

    # killed: no heartbeat for more than 10 s
    clock.advance(2)
    assert task.check_live_results()
    status = _status(task)
    assert TIMED_OUT in status and "2 failed, 1 passed" in status
    assert task.shown_results() == (task.test_results, set())


def test_a_kernel_of_another_pyics_is_ignored_with_the_reason(
    task: Task, notebook: Path, clock: FakeClock
) -> None:
    directory = channel_dir(notebook)
    directory.mkdir(parents=True)
    kernel_file = {"pyics_live": PROTOCOL + 1, "pyics_version": "9.0.0", "pid": 42}
    (directory / "kernel-future.json").write_text(json.dumps(kernel_file))
    (directory / "kernel-future.alive").touch()
    os.utime(directory / "kernel-future.alive", (clock.now, clock.now))

    task.check_live_results()
    status = _status(task)
    assert "Ignoring the notebook kernel (pid 42)" in status
    assert "pyics install_kernel" in status
    assert "2 failed, 1 passed" in status


def test_the_kernel_whose_cell_ran_last_is_shown(
    task: Task, kernel: Callable[..., KernelChannel], task_file: Path, clock: FakeClock
) -> None:
    jupyterlab, vscode = kernel(), kernel()
    clock.advance(1)
    jupyterlab.write_results(1, CELL, {task_file: live_run()})
    clock.advance(1)
    vscode.write_results(1, CellRef("cell-3", 3), {task_file: live_run("failed")})
    task.check_live_results()
    assert _calls(task)["adds"] == "failed"
    status = _status(task)
    assert (
        f"Live results from your notebook kernel with pid {os.getpid()}, the last of 2 "
        f"kernels to run a cell (cell 3, {clock.at()})." in status
    )

    clock.advance(1)
    jupyterlab.write_results(2, CELL, {task_file: live_run()})
    task.check_live_results()
    assert _calls(task)["adds"] == "passed"
    assert f"(cell 2, {clock.at()})" in _status(task)


def test_a_kernel_running_other_tasks_is_said_so(
    task: Task, kernel: Callable[..., KernelChannel], tmp_path: Path, clock: FakeClock
) -> None:
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {tmp_path / "other.pyicstask.json": live_run()})
    task.check_live_results()
    assert "Your notebook kernel doesn't run this task's tests" in _status(task)
    assert task.shown_results()[1] == set()


def test_tests_stay_pending_until_the_saved_notebook_is_tested(
    task_file: Path, kernel: Callable[..., KernelChannel], clock: FakeClock
) -> None:
    task = Task(task_file, setup=False)
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run()})
    assert task.check_live_results()
    assert _status(task) == "tests pending...\n"
    assert not task.new_test_results

    task.check_solved()
    assert "Live results from your notebook kernel" in _status(task)


def test_python_files_have_no_status_line(chdir: Path) -> None:
    add = chdir / "add.py"
    add.write_text("def add(a, b):\n    return 0\n")
    task_file = Task.create_task_json(
        chdir / "add", "Add", "", add, tests=SimpleTest("adds", add, "add", 3)
    )
    task = Task(task_file, setup=False)
    task.check_solved()
    assert not task.check_live_results()
    assert "notebook" not in _status(task)
    assert not (chdir / ".pyics").exists()


# --- tracing ---


def test_t_asks_the_notebook_kernels_to_trace(
    task: Task, notebook: Path, no_new_threads: None
) -> None:
    control = channel_dir(notebook) / "control.json"
    # a TUI that quit while tracing
    control.parent.mkdir(parents=True)
    control.write_text(json.dumps({"tracing": True, "paused": False}))
    task.check_live_results()
    assert read_json(control) == {"tracing": False, "paused": False}

    task.toggle_tracing()
    assert task.tracing
    assert read_json(control) == {"tracing": True, "paused": False}
    assert KernelChannel(notebook).read_control().tracing
    _wait_for(lambda: "Tracing failed tests with snoop" in _status(task))

    task.toggle_tracing()
    assert read_json(control) == {"tracing": False, "paused": False}


def test_watching_a_notebook_creates_no_channel(task: Task, notebook: Path) -> None:
    task.check_live_results()
    assert not channel_dir(notebook).parent.exists()


# --- robustness ---


@pytest.mark.parametrize(
    "contents",
    ["", "{", "[1, 2]", '{"pyics_live": 1}', "\x00\xff"],
    ids=["empty", "half-written", "a list", "incomplete", "binary"],
)
def test_broken_channel_files_are_ignored(
    task: Task, notebook: Path, clock: FakeClock, contents: str
) -> None:
    directory = channel_dir(notebook)
    directory.mkdir(parents=True)
    for name in ("kernel-k1.json", "results-k1.json", "control.json"):
        (directory / name).write_text(contents, encoding="utf-8")
    (directory / "kernel-k1.alive").touch()
    os.utime(directory / "kernel-k1.alive", (clock.now, clock.now))
    task.check_live_results()
    assert NO_KERNEL in _status(task)


def test_errors_reading_the_channel_never_reach_the_tui(
    task: Task,
    notebook: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_new_threads: None,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("locked")

    monkeypatch.setattr(ChannelReader, "poll", fail)
    monkeypatch.setattr(ChannelReader, "write_control", fail)
    assert not task.check_live_results()
    task.toggle_tracing()  # still traces the tests run on the saved notebook
    assert task.tracing


def test_a_channel_that_is_a_file_is_ignored(task: Task, notebook: Path) -> None:
    channel_dir(notebook).parent.mkdir()
    channel_dir(notebook).write_text("not a directory")
    task.check_live_results()
    assert NO_KERNEL in _status(task)


# --- the app ---


def _rendered(app: PyIC) -> str:
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(app.cli.main_content)
    return console.export_text()


def test_the_app_shows_live_results_and_progress_from_saves(
    manifest: Path,
    notebook: Path,
    task_file: Path,
    kernel: Callable[..., KernelChannel],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    no_new_threads: None,
) -> None:
    monkeypatch.setattr("pyics.notebook.kernelspec.is_installed", lambda: True)
    course = Course.load_course_manifest(manifest)
    app = PyIC(CLI())

    def update_until(text: str) -> None:
        _wait_for(lambda: (app._update_course(course), text in _rendered(app))[1])

    update_until(NO_KERNEL)
    assert app.cli.info_text == "Implement add."
    k = kernel()
    clock.advance(1)
    k.write_results(1, CELL, {task_file: live_run()})
    update_until(SOLVED_LIVE)
    assert app.cli.percentage == 0

    clock.advance(1)
    _save(notebook, SOLUTION, clock.now)
    update_until(SAVED_LATER)
    _wait_for(lambda: (app._update_course(course), app.cli.percentage == 0.5)[1])


# --- installing the kernel ---


def test_the_header_suggests_installing_the_kernel(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, no_new_threads: None
) -> None:
    checks: list[bool] = []

    def is_installed() -> bool:
        checks.append(False)
        return False

    monkeypatch.setattr("pyics.notebook.kernelspec.is_installed", is_installed)
    course = Course.load_course_manifest(manifest)
    app = PyIC(CLI())
    app._update_course(course)
    # the instructions stay, the hint is a second, dim line
    assert app.cli.info_text == f"Implement add.\n[dim]{KERNEL_HINT}[/dim]"
    assert "pyics install_kernel" in KERNEL_HINT
    for _ in range(3):
        app._update_course(course)
        assert course.kernel_hint == KERNEL_HINT
    assert checks == [False]  # once per course


def test_no_hint_when_the_kernel_is_installed(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, no_new_threads: None
) -> None:
    monkeypatch.setattr("pyics.notebook.kernelspec.is_installed", lambda: True)
    course = Course.load_course_manifest(manifest)
    app = PyIC(CLI())
    app._update_course(course)
    assert app.cli.info_text == "Implement add."
    assert course.kernel_hint is None


def test_no_hint_for_tasks_without_notebooks(
    task_file: Path,
    chdir: Path,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_new_threads: None,
) -> None:
    checks: list[bool] = []
    monkeypatch.setattr(
        "pyics.notebook.kernelspec.is_installed", lambda: checks.append(False)
    )
    # on the Python task, once the notebook task is solved
    tasks = {"Add": task_file, "Double": chdir / ".tasks/double.pyicstask.json"}
    course = Course(name="c", tasks=tasks, progress=[1, 0])
    assert course.instructions == "Implement double."
    assert course.kernel_hint is None
    assert checks == []
