"""
Test tracing the code under test with snoop, which students can turn on to see what
their code does in the tests that fail.
"""

import contextlib
import json as std_json
import sys
from pathlib import Path

import pytest

import pyics.util.json_handling as json
from pyics.tests.pytest_integration import LiveTestCollectionPlugin, RichFailureRepr
from pyics.tests.tests import PrintTest, SimpleTest, SpecTest, StructureTest, Test
from pyics.tests.trace import Trace

SOLUTION = """
def double(x):
    return 2 * x

def total(xs):
    result = 0
    for x in xs:
        result += double(x)
    return result

def countdown(n):
    return 0 if n == 0 else countdown(n - 1)

def fail():
    raise ValueError("boom")
"""

SCRIPT = """
def greet(name):
    return f"Hello, {name}!"

print(greet(input("Name: ")))
"""


@pytest.fixture
def solution(tmp_path: Path) -> Path:
    path = tmp_path / "solution.py"
    path.write_text(SOLUTION)
    return path


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "script.py"
    path.write_text(SCRIPT)
    return path


def _trace(test: Test, trace: Trace | None = None) -> str:
    """Run ``test`` with ``trace`` (a new one by default), and return the trace as
    plain text, whether the test passes or not."""
    trace = trace or Trace()
    with contextlib.suppress(Exception):
        test.run(trace=trace)
    return trace.text.plain


# --- tracing tests ---


def test_function_calls_are_traced_with_the_calls_they_make(solution: Path) -> None:
    trace = _trace(SimpleTest("t", solution, "total", 0, function_args=[[1, 2]]))
    assert '>>> Call to total in File "solution.py", line 5' in trace
    assert "xs = [1, 2]" in trace
    assert ">>> Call to double" in trace
    assert "<<< Return value from double: 4" in trace
    assert "<<< Return value from total: 6" in trace
    # importing the file isn't traced
    assert "<module>" not in trace


def test_recursion_is_traced_at_any_depth(solution: Path) -> None:
    trace = _trace(
        SimpleTest("t", solution, "countdown", 0, function_args=[30]),
        Trace(max_lines=10_000),
    )
    assert trace.count(">>> Call to countdown") == 31
    assert "n = 0" in trace


def test_scripts_are_traced_as_a_whole(script: Path) -> None:
    test = PrintTest("t", script, "Name: Bob\nHello, Bob!", inputs=["Bob"])
    trace = _trace(test)
    assert '>>> Call to <module> in File "script.py", line 1' in trace
    assert "name = 'Bob'" in trace
    assert "<<< Return value from greet: 'Hello, Bob!'" in trace
    # but not pyics itself, e.g. its stand-in for input(), or the module's dunders
    assert "_ThreadLocalInput" not in trace
    assert "__builtins__" not in trace

    # a traced test still passes or fails as it would otherwise
    test.run(trace=Trace())


def test_exceptions_are_traced_and_tracing_ends(solution: Path) -> None:
    previous = sys.gettrace()
    trace = Trace()
    with pytest.raises(ValueError, match="boom"):
        SimpleTest("t", solution, "fail", None).run(trace=trace)
    assert "ValueError: boom" in trace.text.plain
    assert sys.gettrace() is previous


def test_tests_whose_author_turned_tracing_off_arent_traced(
    solution: Path, script: Path
) -> None:
    simple = SimpleTest("t", solution, "double", 0, function_args=[2], traceable=False)
    assert _trace(simple) == ""
    assert _trace(PrintTest("t", script, "", inputs=["Bob"], traceable=False)) == ""


def test_spec_and_structure_tests_ignore_the_trace(solution: Path) -> None:
    def double(y): ...

    assert _trace(SpecTest("t", solution, "double", double)) == ""
    rules = {"concept": "loop", "scope": "total"}
    assert _trace(StructureTest("t", solution, rules)) == ""


def test_long_traces_keep_their_first_and_last_lines(solution: Path) -> None:
    test = SimpleTest("t", solution, "total", 0, function_args=[list(range(100))])
    lines = [line.strip() for line in _trace(test, Trace(max_lines=10)).split("\n")]
    assert len(lines) == 11
    assert lines[0].startswith(">>> Call to total")
    assert lines[5].startswith("... ") and lines[5].endswith(" lines omitted ...")
    assert lines[-1] == "<<< Return value from total: 9900"


# --- storing tests ---


def test_traceable_is_stored(solution: Path) -> None:
    test = SimpleTest("t", solution, "double", 4, traceable=False)
    loaded = json.loads(test.serialize())
    assert loaded.traceable is False


def test_tests_stored_without_traceable_are_traceable(
    solution: Path, script: Path
) -> None:
    for test in [SimpleTest("t", solution, "double", 4), PrintTest("t", script, "")]:
        stored = std_json.loads(test.serialize())
        del stored["attributes"]["traceable"]
        loaded = json.loads(std_json.dumps(stored))
        assert "traceable" not in vars(loaded)
        assert loaded.traceable is True


# --- running tests through pytest ---


def _failures(
    tests: list[Test], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracing: bool
) -> dict[str, RichFailureRepr]:
    """Run ``tests`` like a ``Task`` does, and return their failures by test name."""
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    plugin = LiveTestCollectionPlugin(tests, tracing=tracing)
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
    failures = {}
    for report in plugin.reports:
        if report.when == "call" and report.failed:
            assert isinstance(report.longrepr, RichFailureRepr)
            failures[report.nodeid.removeprefix("::")] = report.longrepr
    return failures


def test_failed_tests_show_their_trace_or_why_there_is_none(
    solution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def double(y): ...

    tests: list[Test] = [
        SimpleTest("passes", solution, "double", 4, function_args=[2]),
        SimpleTest("fails", solution, "double", 5, function_args=[2]),
        SimpleTest("hidden", solution, "double", 5, function_args=[2], traceable=False),
        SimpleTest("missing", solution, "triple", 6, function_args=[2]),
        SpecTest("spec", solution, "double", double),
    ]
    failures = _failures(tests, tmp_path, monkeypatch, tracing=True)

    assert list(failures) == ["fails", "hidden", "missing", "spec"]
    traces = {name: f.trace.plain for name, f in failures.items() if f.trace is not None}
    assert list(traces) == list(failures)
    assert "<<< Return value from double: 4" in traces["fails"]
    assert traces["hidden"] == "Tracing is turned off for this test."
    assert traces["missing"] == (
        "Nothing was traced: the test failed before running your code."
    )
    assert traces["spec"] == "SpecTests can't be traced."
    # the trace isn't part of the failure message, shown in the test summary
    assert failures["fails"].text.plain == "fails failed: 4 == 5"


def test_failed_tests_arent_traced_by_default(
    solution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests: list[Test] = [SimpleTest("fails", solution, "double", 5, function_args=[2])]
    failures = _failures(tests, tmp_path, monkeypatch, tracing=False)
    assert failures["fails"].trace is None
