"""
The seams for plugins (``pyics.plugins``): a results overlay changes which results a
task's status shows, but never its progress, and ``LIVE_SOURCE`` lets tests take the
code under test from elsewhere than the saved file.
"""

import dataclasses
import io
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pluggy
import pytest
from rich.console import Console, RenderableType
from rich.text import Text

from pyics import plugins
from pyics.data import Course, Task
from pyics.tests.pytest_integration import LiveTestCollectionPlugin
from pyics.tests.results import TestResult
from pyics.tests.tests import LIVE_SOURCE, PrintTest, SimpleTest, StructureTest

TIMEOUT = 10  # seconds
# returns 0, so that ``add(1, 2) == 3`` fails, and ``add(0, 0) == 0`` passes
WRONG = "def add(a, b):\n    return 0\n"
LINE = "Results from a fake kernel."


class FakeOverlay:
    """An overlay that shows the tests in ``outcomes`` (by index) with the outcome given
    there, labelled as its own, and says so below the summary."""

    def __init__(self) -> None:
        self.outcomes: dict[int | None, str] = {}
        self.new = False
        # the saved results and their times it was applied to
        self.applied: list[tuple[list[TestResult], float | None]] = []

    def poll(self) -> bool:
        new, self.new = self.new, False
        return new

    def apply(
        self, saved: list[TestResult], saved_time: float | None
    ) -> plugins.Overlay:
        self.applied.append((saved, saved_time))
        results = []
        for result in saved:
            outcome = self.outcomes.get(result.index) if result.when == "call" else None
            if outcome is not None:
                result = dataclasses.replace(
                    result,
                    nodeid=f"{result.nodeid} (fake)",
                    outcome=outcome,
                    message=None if outcome == "passed" else "fails in the fake kernel",
                )
            results.append(result)
        return plugins.Overlay(results, [Text(LINE)])


class FakePlugin:
    """A plugin that gives each task ``overlay``."""

    def __init__(self, overlay: FakeOverlay | None) -> None:
        self.overlay = overlay
        self.asked: list[Task] = []

    @plugins.hookimpl
    def pyics_results_overlay(self, task: Task) -> FakeOverlay | None:
        self.asked.append(task)
        return self.overlay


@pytest.fixture
def manager() -> Iterator[pluggy.PluginManager]:
    """The plugin manager, without the plugins tests register on it once they end."""
    plugins.manager.cache_clear()
    yield plugins.manager()
    plugins.manager.cache_clear()


@pytest.fixture
def exercise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # pytest runs started by the tests write their cache to the working directory
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "add.py"
    path.write_text(WRONG)
    return path


@pytest.fixture
def task_file(exercise: Path) -> Path:
    """A task whose first test fails on ``exercise``, and whose second one passes."""
    tests = [
        SimpleTest("adds", exercise, "add", 3, function_args=[1, 2]),
        SimpleTest("zero", exercise, "add", 0, function_args=[0, 0]),
    ]
    return Task.create_task_json(
        exercise.parent / "add", "Add", "", exercise, None, tests
    )


def _text(renderable: RenderableType) -> str:
    console = Console(width=100, file=io.StringIO(), record=True)
    console.print(renderable)
    return console.export_text()


def _wait_for(condition: Callable[[], object]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# --- results overlays ---


def test_an_overlay_changes_the_status_but_not_the_progress(
    manager: pluggy.PluginManager, task_file: Path
) -> None:
    overlay = FakeOverlay()
    overlay.outcomes = {0: "passed"}
    plugin = FakePlugin(overlay)
    manager.register(plugin)
    task = Task(task_file, setup=False)
    assert not task.check_solved()
    saved = _text(task.status)
    assert "adds failed" in saved and LINE not in saved

    # the first check asks for the overlay, and shows it
    task.clear_test_results()
    assert task.check_overlay()
    assert task.new_test_results
    shown = _text(task.status)
    assert "100%" in shown and "2 passed" in shown and "adds failed" not in shown
    assert LINE in shown
    assert overlay.applied == [(task.test_results, task.test_results_time)]
    assert not task.solved

    # the overlay's failures are shown under its labels
    overlay.outcomes = {1: "failed"}
    task.clear_test_results()
    assert not task.check_overlay()  # nothing new yet
    assert not task.new_test_results
    overlay.new = True
    assert task.check_overlay()
    assert task.new_test_results
    shown = _text(task.status)
    assert "FAILED ::zero (fake) - fails in the fake kernel" in shown
    assert "adds failed" in shown

    assert plugin.asked == [task]  # once per task


def test_without_an_overlay_the_status_is_unchanged(
    manager: pluggy.PluginManager, task_file: Path
) -> None:
    def status() -> RenderableType:
        task = Task(task_file, setup=False)
        task.check_solved()
        task.clear_test_results()
        shown = task.status
        assert not task.check_overlay()
        assert task.status is shown and not task.new_test_results
        return shown

    without_plugins = _text(status())
    plugin = FakePlugin(None)  # has no overlay for the task
    manager.register(plugin)
    assert _text(status()) == without_plugins
    assert len(plugin.asked) == 1


def test_tests_stay_pending_until_the_saved_files_are_tested(
    manager: pluggy.PluginManager, task_file: Path
) -> None:
    overlay = FakeOverlay()
    manager.register(FakePlugin(overlay))
    task = Task(task_file, setup=False)
    task.check_overlay()
    overlay.new = True
    task.check_overlay()
    assert _text(task.status).strip() == "tests pending..."
    assert not task.new_test_results and not overlay.applied

    task.check_solved()
    assert LINE in _text(task.status)


def test_courses_show_the_overlays_new_results(
    manager: pluggy.PluginManager, task_file: Path
) -> None:
    overlay = FakeOverlay()
    manager.register(FakePlugin(overlay))
    threads = set(threading.enumerate())
    course = Course.load_course_manifest(
        Course.create_course_manifest(task_file.parent / "course", [task_file])
    )

    def ticked_until(text: str) -> bool:
        new_results, _ = course.tick()
        if new_results:
            course.clear_test_results()
        return text in _text(course.status)

    _wait_for(lambda: ticked_until(LINE))
    assert "adds failed" in _text(course.status)

    overlay.outcomes = {0: "passed"}
    overlay.new = True
    assert course.tick()[0]
    assert "100%" in _text(course.status)
    assert course.progress == 0  # the saved file still fails

    for thread in set(threading.enumerate()) - threads:
        thread.join(TIMEOUT)


# --- live sources ---


class LiveModules:
    """A live source holding ``module`` as the code of the file at ``path``."""

    def __init__(self, path: Path, module: ModuleType) -> None:
        self.path = path
        self.live_module = module

    def module(self, path: Path) -> ModuleType | None:
        return self.live_module if Path(path) == self.path else None


def test_tests_take_their_code_from_the_live_source(
    exercise: Path, tmp_path: Path
) -> None:
    live = ModuleType("live")
    live.add = lambda a, b: a + b  # type: ignore[attr-defined]
    other = tmp_path / "other.py"
    other.write_text(WRONG)
    tests = [
        SimpleTest("live", exercise, "add", 3, function_args=[1, 2]),
        SimpleTest("other", other, "add", 0, function_args=[1, 2]),  # loaded as usual
        PrintTest("prints", exercise, ""),
        StructureTest("no loops", exercise, {"concept": "loop", "max": 0}),
    ]
    plugin = LiveTestCollectionPlugin(tests)
    token = LIVE_SOURCE.set(LiveModules(exercise, live))
    try:
        assert tests[0].load_solution() is live
        pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
    finally:
        LIVE_SOURCE.reset(token)

    calls = [
        (r.nodeid, r.outcome, r.message) for r in plugin.results if r.when == "call"
    ]
    assert calls == [
        ("::live", "passed", None),
        ("::other", "passed", None),
        ("::prints", "skipped", "Skipped: runs when the notebook is saved"),
        ("::no loops", "skipped", "Skipped: runs when the notebook is saved"),
    ]

    # without it, the saved file is tested
    assert tests[0].load_solution() is not live
    with pytest.raises(AssertionError):
        tests[0].run()
