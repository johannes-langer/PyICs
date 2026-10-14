"""
Test ``GeneralTest``s: tests written as pytest code, whose tests pytest collects from
their source (see ``pyics.tests.general``), each a result of its own.
"""

import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from notebook_helpers import write_notebook

import pyics.util.json_handling as json
from pyics.data import Task
from pyics.tests import GeneralTest
from pyics.tests.pytest_integration import run_tests
from pyics.tests.results import TestResult
from pyics.tests.tests import LIVE_SOURCE, SimpleTest, check_source

EXERCISE = """
def add(a, b):
    return a + b

def add_item(item, items=[]):
    items.append(item)
    return items

def greet():
    name = input("Name: ")
    print(f"Hello, {name}!")

if __name__ == "__main__":
    greet()
"""

SOURCE = """
import pytest

@pytest.mark.parametrize(("a", "b", "total"), [(1, 2, 3), (2, 2, 5)])
def test_add(solution, a, b, total):
    assert solution.add(a, b) == total

class TestItems:
    def test_fresh_list(self, solution):
        assert solution.add_item(1) == [1]
"""


@pytest.fixture(autouse=True)
def chdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """pytest writes its cache to the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def exercise(tmp_path: Path) -> Path:
    path = tmp_path / "ex.py"
    path.write_text(EXERCISE)
    return path


def _calls(results: list[TestResult]) -> list[tuple[str, str]]:
    return [(r.nodeid, r.outcome) for r in results if r.when == "call"]


def _messages(results: list[TestResult]) -> dict[str, str]:
    return {
        r.nodeid: r.message_text.plain
        for r in results
        if r.when == "call" and r.message is not None
    }


def _files(directory: Path) -> list[str]:
    """The names of the files in ``directory``, besides pytest's cache."""
    return sorted(p.name for p in directory.iterdir() if p.name != ".pytest_cache")


def _run(source: str, exercise: Path, **kwargs) -> list[TestResult]:
    return run_tests([GeneralTest("general", exercise, source, **kwargs)])


# --- results and messages ---


def test_each_collected_test_is_a_result(exercise: Path) -> None:
    results = run_tests(
        [
            SimpleTest("simple", exercise, "add", 3, function_args=[1, 2]),
            GeneralTest("general", exercise, SOURCE),
        ]
    )
    assert _calls(results) == [
        ("::simple", "passed"),
        ("::general::test_add[1-2-3]", "passed"),
        ("::general::test_add[2-2-5]", "failed"),
        ("::general::TestItems::test_fresh_list", "passed"),
    ]
    # all results of a GeneralTest share its index
    assert [r.index for r in results if r.when == "call"] == [0, 1, 1, 1]


def test_failures_are_explained_by_assertion_rewriting(exercise: Path) -> None:
    message = _messages(_run(SOURCE, exercise))["::general::test_add[2-2-5]"]
    assert "assert solution.add(a, b) == total" in message  # the source line
    assert "assert 4 == 5" in message
    assert "where 4 = <function add" in message


def test_json_round_trip_through_a_task(exercise: Path, tmp_path: Path) -> None:
    test = GeneralTest("general", exercise, SOURCE, isolated=True)
    loaded = json.loads(test.serialize())
    assert isinstance(loaded, GeneralTest)
    assert vars(loaded) == vars(test)

    task_file = Task.create_task_json(tmp_path / "t", "t", "", exercise, None, [test])
    task = Task(task_file, setup=False)
    assert not task.check_solved()
    assert _calls(task.test_results) == [
        ("::general::test_add[1-2-3]", "passed"),
        ("::general::test_add[2-2-5]", "failed"),
        ("::general::TestItems::test_fresh_list", "passed"),
    ]

    exercise.write_text(EXERCISE.replace("a + b", "5 if a == 2 else a + b"))
    assert task.check_solved()


def test_run_raises_for_failed_tests(exercise: Path) -> None:
    with pytest.raises(AssertionError) as excinfo:
        GeneralTest("general", exercise, SOURCE).run()
    lines = str(excinfo.value).splitlines()
    assert lines[0].startswith("::general::test_add[2-2-5] failed: ")
    GeneralTest("passes", exercise, "def test_x(solution):\n    pass\n").run()


def test_check_source() -> None:
    assert check_source("def test_x(): pass\n")
    assert check_source("class TestX:\n    def test_y(self): pass\n")
    with pytest.raises(ValueError, match="defines no tests"):
        check_source("def helper(): pass\n")
    with pytest.raises(SyntaxError):
        check_source("def test_x(:\n")
    with pytest.raises(ValueError):
        GeneralTest("t", "ex.py", "x = 1\n")


# --- fixtures ---


def test_solution_and_its_helpers(exercise: Path) -> None:
    source = """
from pyics.testing import capture_stdout, provide_input

def test_solution(solution):
    assert solution.add(1, 2) == 3
    assert solution.__name__ == "current_solution"

def test_load_solution_loads_anew(load_solution):
    first, second = load_solution(), load_solution()
    assert first is not second
    assert first.add_item(1) == [1]
    assert second.add_item(1) == [1]  # a fresh default list

def test_run_solution(run_solution):
    assert run_solution(["Bob"]) == "Name: Bob\\nHello, Bob!\\n"

def test_exercise_path(exercise_path):
    assert exercise_path.is_absolute()
    assert exercise_path.read_text().startswith("\\ndef add")

def test_testing_helpers(solution):
    with capture_stdout() as out, provide_input(["Ann"]):
        solution.greet()
    assert out.getvalue() == "Name: Ann\\nHello, Ann!\\n"
"""
    results = _run(source, exercise)
    assert _messages(results) == {}
    assert len(_calls(results)) == 5


def test_teardown_and_temporary_files_are_cleaned_up(
    exercise: Path, chdir: Path
) -> None:
    source = """
import os
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield
    Path("written.txt").unlink(missing_ok=True)

@pytest.fixture
def log():
    lines = []
    yield lines
    Path(os.environ["PYICS_LOG"]).write_text("\\n".join(lines))

def test_writes(log, solution):
    Path("written.txt").write_text("x")
    log.append("ran")
    assert solution.add(1, 1) == 2
"""
    log = chdir.parent / f"{chdir.name}.log"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYICS_LOG", str(log))
        results = _run(source, exercise)
    assert _calls(results) == [("::general::test_writes", "passed")]
    assert log.read_text() == "ran"
    assert _files(chdir) == ["ex.py"]


def test_isolated_tests_run_in_a_temporary_directory(
    exercise: Path, chdir: Path
) -> None:
    source = """
from pathlib import Path

def test_writes(solution, exercise_path):
    assert Path.cwd() != exercise_path.parent
    assert list(Path().iterdir()) == []
    Path("written.txt").write_text("x")
"""
    assert _calls(_run(source, exercise, isolated=True)) == [
        ("::general::test_writes", "passed")
    ]
    assert _files(chdir) == ["ex.py"]


def test_capsys_captures_only_its_own_thread(
    exercise: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.g. the CLI keeps drawing while a task's tests run in the background. pyics's
    capsys takes precedence over pytest's, which would capture all threads."""
    sync = SimpleNamespace(printed=threading.Event(), resume=threading.Event())
    monkeypatch.setitem(sys.modules, "sync", sync)  # imported by the test
    source = """
import sync
from pyics.tests.general import ThreadCapture

def test_prints(capsys):
    assert isinstance(capsys, ThreadCapture)
    print("Hello")
    sync.printed.set()
    sync.resume.wait(5)
    print("Bye", file=__import__("sys").stderr)
    assert capsys.readouterr() == ("Hello\\n", "Bye\\n")
    assert capsys.readouterr() == ("", "")
"""
    with ThreadPoolExecutor() as pool:
        run = pool.submit(_run, source, exercise)
        assert sync.printed.wait(5)
        print("drawn meanwhile")
        sync.resume.set()
        results = run.result(timeout=10)

    assert _messages(results) == {}
    assert _calls(results) == [("::general::test_prints", "passed")]
    assert capsys.readouterr().out == "drawn meanwhile\n"


# --- failures that must count ---


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def test_x(:\n    pass\n", "can't be parsed: invalid syntax (line 1)"),
        ("def helper():\n    pass\n", "has no tests"),
        ("raise ValueError('at import')\ndef test_x():\n    pass\n", "at import"),
    ],
)
def test_broken_sources_fail_as_one_result(
    exercise: Path, source: str, message: str
) -> None:
    test = GeneralTest("general", exercise, "def test_x():\n    pass\n")
    test.source = source  # e.g. stored by hand
    simple = SimpleTest("simple", exercise, "add", 2, function_args=[1, 1])
    results = run_tests([test, simple])
    assert _calls(results) == [("::general", "failed"), ("::simple", "passed")]
    assert message in _messages(results)["::general"]


def test_an_exercise_file_that_raises_fails_its_tests(exercise: Path) -> None:
    exercise.write_text("raise RuntimeError('unfinished')\n")
    results = _run(SOURCE, exercise)
    # the setup fails, which counts as the call
    assert [o for _, o in _calls(results)] == ["failed"] * 3
    message = _messages(results)["::general::TestItems::test_fresh_list"]
    assert "RuntimeError: unfinished" in message
    assert "ex.py" in message
    assert "general.py" not in message  # pyics's frames are hidden


def test_a_failing_teardown_fails_the_test(exercise: Path) -> None:
    source = """
import pytest

@pytest.fixture
def broken():
    yield
    raise RuntimeError("teardown")

def test_x(broken):
    pass
"""
    results = _run(source, exercise)
    assert _calls(results) == [("::general::test_x", "failed")]
    assert "RuntimeError: teardown" in _messages(results)["::general::test_x"]


def test_skipped_tests_dont_count_as_passed(exercise: Path, tmp_path: Path) -> None:
    source = "import pytest\n\ndef test_x():\n    pytest.skip('later')\n"
    test = GeneralTest("general", exercise, source)
    task_file = Task.create_task_json(tmp_path / "t", "t", "", exercise, None, test)
    task = Task(task_file, setup=False)
    assert not task.check_solved()
    assert _calls(task.test_results) == [("::general::test_x", "skipped")]


# --- tracing, notebooks and live runs ---


def test_failed_tests_are_traced(exercise: Path) -> None:
    source = "def test_add(solution):\n    assert solution.add(1, 2) == 4\n"
    [result] = [
        r
        for r in run_tests([GeneralTest("general", exercise, source)], tracing=True)
        if r.when == "call"
    ]
    assert result.trace_text is not None
    assert "return a + b" in result.trace_text.plain

    untraced = GeneralTest("general", exercise, source, traceable=False)
    [result] = [r for r in run_tests([untraced], tracing=True) if r.when == "call"]
    assert result.trace_text is not None
    assert result.trace_text.plain == "Tracing is turned off for this test."


def test_notebook_exercise_files(tmp_path: Path) -> None:
    nb = write_notebook(
        tmp_path / "ex.ipynb",
        ["def add(a, b):\n    return a + b", "undefined_name", "print('done')"],
    )
    source = """
def test_add(solution):
    assert solution.add(1, 2) == 3

def test_output(run_solution):
    assert run_solution() == "done\\n"

def test_fails(solution):
    assert solution.add(1, 1) == 3
"""
    results = run_tests([GeneralTest("general", nb, source)])
    assert _calls(results) == [
        ("::general::test_add", "passed"),
        ("::general::test_output", "passed"),
        ("::general::test_fails", "failed"),
    ]
    # the error in the second cell is named, as it may be why the test failed
    assert "cell 2 raised NameError" in _messages(results)["::general::test_fails"]


class _LiveModules:
    def __init__(self, path: Path, module: ModuleType) -> None:
        self.path = path
        self.live_module = module

    def module(self, path: Path) -> ModuleType | None:
        return self.live_module if Path(path).resolve() == self.path else None


@pytest.fixture
def live_source(exercise: Path) -> Iterator[ModuleType]:
    live = ModuleType("live")
    live.add = lambda a, b: a + b + 100  # type: ignore[attr-defined]
    token = LIVE_SOURCE.set(_LiveModules(exercise.resolve(), live))
    try:
        yield live
    finally:
        LIVE_SOURCE.reset(token)


def test_live_runs_skip_what_runs_the_saved_file(
    exercise: Path, live_source: ModuleType
) -> None:
    source = """
def test_live(solution):
    assert solution.add(1, 2) == 103

def test_load(load_solution):
    load_solution()

def test_run(run_solution):
    run_solution()
"""
    results = _run(source, exercise)
    assert _calls(results) == [
        ("::general::test_live", "passed"),
        ("::general::test_load", "skipped"),
        ("::general::test_run", "skipped"),
    ]
    assert _messages(results)["::general::test_load"] == (
        "Skipped: runs when the notebook is saved"
    )
