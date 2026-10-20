"""
Soft tests (``Test.soft``), whose failures are warnings that don't keep their task
unsolved, and courses with ``free_navigation``, whose tasks can be left unsolved.
"""

import io
import json
from pathlib import Path

import pytest
from rich.console import Console, RenderableType
from rich.text import Text

from pyics.data import Course, Task
from pyics.tests.pytest_integration import run_tests
from pyics.tests.results import TestResult
from pyics.tests.tests import (
    GeneralTest,
    PrintTest,
    SimpleTest,
    SpecTest,
    StructureTest,
    Test,
)
from pyics.util import json_handling

SOLUTION = "def double(x):\n    return 2 * x\n"

GENERAL_SOURCE = """
def test_passes(solution):
    assert solution.double(1) == 2

def test_fails(solution):
    assert solution.double(1) == 3
"""


@pytest.fixture
def solution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    path = tmp_path / "solution.py"
    path.write_text(SOLUTION)
    return path


def _ansi(renderable: RenderableType) -> str:
    """``renderable`` as a terminal shows it, with ANSI escape codes."""
    console = Console(
        file=io.StringIO(), force_terminal=True, color_system="truecolor", width=200
    )
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]


def _task(tmp_path: Path, solution: Path, tests: list[Test]) -> Task:
    task_file = Task.create_task_json(tmp_path / "t", "t", "", solution, None, tests)
    return Task(task_file, setup=False)


# --- the tests ---


def test_every_kind_of_test_can_be_soft(solution: Path) -> None:
    tests: list[Test] = [
        PrintTest("p", solution, "", soft=True),
        SimpleTest("s", solution, "double", 2, function_args=[1], soft=True),
        GeneralTest("g", solution, GENERAL_SOURCE, soft=True),
        SpecTest("spec", solution, "double", {"is_class": False}, soft=True),
        StructureTest("st", solution, {"concept": "loop", "max": 0}, soft=True),
    ]
    assert all(test.soft for test in tests)


def test_soft_is_only_stored_if_set(solution: Path, tmp_path: Path) -> None:
    hard = SimpleTest("hard", solution, "double", 2, function_args=[1])
    soft = SimpleTest("soft", solution, "double", 2, function_args=[1], soft=True)
    assert "soft" not in vars(hard) and hard.soft is False

    task_file = Task.create_task_json(
        tmp_path / "t", "t", "", solution, None, [hard, soft]
    )
    stored = json_handling.load_plain(task_file)["tests"]
    assert [test["attributes"].get("soft") for test in stored] == [None, True]

    loaded = Task.read_task_json(task_file)["tests"]
    assert [test.soft for test in loaded] == [False, True]


# --- the results ---


def test_results_of_soft_tests_are_soft(solution: Path) -> None:
    results = run_tests(
        [
            SimpleTest("hard", solution, "double", 3, function_args=[1]),
            SimpleTest("soft", solution, "double", 3, function_args=[1], soft=True),
            GeneralTest("general", solution, GENERAL_SOURCE, soft=True),
        ]
    )
    calls = [(r.nodeid, r.outcome, r.soft) for r in results if r.when == "call"]
    assert calls == [
        ("::hard", "failed", False),
        ("::soft", "failed", True),
        ("::general::test_passes", "passed", True),
        ("::general::test_fails", "failed", True),
    ]


def test_soft_survives_json_and_defaults_to_false() -> None:
    result = TestResult("::t", "call", "failed", 0.1, message="t failed", soft=True)
    assert TestResult.from_json(json.loads(json.dumps(result.to_json()))) == result

    data = {"nodeid": "::t", "when": "call", "outcome": "failed", "duration": 1}
    assert TestResult.from_json(data).soft is False
    for malformed in (1, "yes", None):
        with pytest.raises(ValueError):
            TestResult.from_json({**data, "soft": malformed})


# --- the task ---


def test_a_failing_soft_test_doesnt_keep_the_task_unsolved(
    solution: Path, tmp_path: Path
) -> None:
    task = _task(
        tmp_path,
        solution,
        [
            SimpleTest("passes", solution, "double", 2, function_args=[1]),
            SimpleTest("warns", solution, "double", 3, function_args=[1], soft=True),
        ],
    )
    assert task.check_solved()

    shown = _ansi(task.status)
    text = Text.from_ansi(shown).plain
    assert "warns (soft)" in text
    assert "SOFT-FAILED ::warns - warns failed: 2 == 3" in text
    assert "1 soft-failed, 1 passed" in text
    assert "50%" in text
    # bright yellow, not bright red
    assert "\x1b[93m" in shown and "\x1b[91m" not in shown


def test_a_failing_hard_test_keeps_the_task_unsolved(
    solution: Path, tmp_path: Path
) -> None:
    task = _task(
        tmp_path,
        solution,
        [
            SimpleTest("fails", solution, "double", 3, function_args=[1]),
            SimpleTest("warns", solution, "double", 3, function_args=[1], soft=True),
        ],
    )
    assert not task.check_solved()
    text = Text.from_ansi(_ansi(task.status)).plain
    assert "FAILED ::fails" in text
    assert "1 failed, 1 soft-failed" in text


def test_a_task_of_only_failing_soft_tests_is_solved(
    solution: Path, tmp_path: Path
) -> None:
    test = GeneralTest("general", solution, GENERAL_SOURCE, soft=True)
    assert _task(tmp_path, solution, [test]).check_solved()


# --- free navigation ---


A, B = Path("a.pyicstask.json"), Path("b.pyicstask.json")


class _UnsolvedTask:
    """Stands in for ``Task``: never solved, and runs nothing."""

    solved = False

    def __init__(self, path: Path, *, setup: bool = True, module_policy=None) -> None:
        self.path = path


@pytest.mark.parametrize("free_navigation", [False, True])
def test_free_navigation_moves_on_from_unsolved_tasks(
    monkeypatch: pytest.MonkeyPatch, free_navigation: bool
) -> None:
    monkeypatch.setattr("pyics.data.course.Task", _UnsolvedTask)
    course = Course(
        name="c",
        tasks={"a": A, "b": B},
        progress=[0, 0],
        free_navigation=free_navigation,
    )
    task = course.next()
    assert task is not None
    assert task.path == (B if free_navigation else A)
    # the last task stays the last one
    assert course.next() is task


def test_free_navigation_is_stored_in_the_manifest_if_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pyics.data.course.Task", _UnsolvedTask)
    plain = Course.create_course_manifest(tmp_path / "plain", [A], use_task_names=False)
    assert "free_navigation" not in json_handling.load_plain(plain)
    assert Course.load_course_manifest(plain).free_navigation is False

    free = Course.create_course_manifest(
        tmp_path / "free", [A], use_task_names=False, free_navigation=True
    )
    assert json_handling.load_plain(free)["free_navigation"] is True
    assert Course.load_course_manifest(free).free_navigation is True

    # kept from the manifest that is rewritten, unless it is changed
    manifest = json_handling.load_plain(free)
    kept = Course.create_course_manifest(
        tmp_path / "kept", [A], use_task_names=False, metadata=manifest
    )
    assert json_handling.load_plain(kept)["free_navigation"] is True
    turned_off = Course.create_course_manifest(
        tmp_path / "off",
        [A],
        use_task_names=False,
        metadata=manifest,
        free_navigation=False,
    )
    assert "free_navigation" not in json_handling.load_plain(turned_off)
