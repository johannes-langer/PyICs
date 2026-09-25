"""
Test whether the automatic tests work.
"""

from pathlib import Path

import pytest

import pyics.util.json_handling as json
from pyics.tests.pytest_integration import LiveTestCollectionPlugin
from pyics.tests.tests import SimpleTest

SOLUTION = """
def add(a, b=0):
    return a + b

def neighbours(edges, node):
    return [b for a, b in edges if a == node]

def path(start, goal):
    return (start, goal)

def fail():
    raise ValueError("boom")
"""

EDGES = [("A", "B"), ("A", "C"), ("B", "C")]


@pytest.fixture
def solution(tmp_path: Path) -> Path:
    path = tmp_path / "solution.py"
    path.write_text(SOLUTION)
    return path


# --- SimpleTest ---


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected"),
    [
        ("add", [1, 2], {}, 3),
        ("add", [1], {}, 1),  # default of b
        ("add", [], {"a": 1, "b": 2}, 3),
        ("add", [1], {"b": 2}, 3),
        ("add", ["a", "b"], {}, "ab"),
        ("neighbours", [EDGES, "A"], {}, ["B", "C"]),
    ],
)
def test_matching_output_passes(
    solution: Path, function_name: str, args: list, kwargs: dict, expected: object
) -> None:
    SimpleTest(
        "t",
        solution,
        function_name,
        expected,
        function_args=args,
        function_kwargs=kwargs,
    ).run()


def test_mismatching_output_fails(solution: Path) -> None:
    test = SimpleTest("t", solution, "add", 4, function_args=[1, 2])
    with pytest.raises(AssertionError, match=r"^3 == 4$"):
        test.run()


def test_failure_explains_the_difference(solution: Path) -> None:
    test = SimpleTest(
        "t", solution, "neighbours", ["B", "D"], function_args=[EDGES, "A"]
    )
    with pytest.raises(AssertionError) as excinfo:
        test.run()
    lines = str(excinfo.value).splitlines()
    assert lines[0] == "['B', 'C'] == ['B', 'D']"
    assert "At index 1 diff: 'C' != 'D'" in lines


def test_output_type_must_match(solution: Path) -> None:
    test = SimpleTest("t", solution, "path", ["A", "B"], function_args=["A", "B"])
    with pytest.raises(AssertionError, match=r"^\('A', 'B'\) == \['A', 'B'\]"):
        test.run()


def test_missing_function_raises(solution: Path) -> None:
    test = SimpleTest("t", solution, "subtract", 1, function_args=[2, 1])
    with pytest.raises(AttributeError, match="subtract"):
        test.run()


def test_errors_in_the_solution_propagate(solution: Path) -> None:
    with pytest.raises(ValueError, match="boom"):
        SimpleTest("t", solution, "fail", None).run()


def test_solution_is_reloaded_on_every_run(solution: Path) -> None:
    test = SimpleTest("t", solution, "add", 3, function_args=[1, 2])
    test.run()

    solution.write_text(SOLUTION.replace("a + b", "a + b + 1"))
    with pytest.raises(AssertionError, match=r"^4 == 3$"):
        test.run()


def test_from_dict(solution: Path) -> None:
    test = SimpleTest.from_dict(
        {
            "name": "t",
            "file_path": solution,
            "function_name": "add",
            "expected": 3,
            "function_kwargs": {"a": 1, "b": 2},
        }
    )
    assert test.function_args == []
    test.run()


def test_json_round_trip(solution: Path) -> None:
    # stored in task files like this, tuples must survive for the output to match
    test = SimpleTest(
        "t",
        solution,
        "path",
        ("A", "C"),
        function_args=["A"],
        function_kwargs={"goal": "C"},
    )
    loaded = json.loads(test.serialize())
    assert isinstance(loaded, SimpleTest)
    assert vars(loaded) == vars(test)
    assert isinstance(loaded.expected, tuple)
    loaded.run()


def test_run_through_pytest(
    solution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run tests like a ``Task`` does, collecting their reports."""
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    plugin = LiveTestCollectionPlugin(
        [
            SimpleTest("passes", solution, "add", 3, function_args=[1, 2]),
            SimpleTest("fails", solution, "add", 4, function_args=[1, 2]),
            SimpleTest("raises", solution, "fail", None),
        ]
    )
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])

    calls = [r for r in plugin.reports if r.when == "call"]
    assert [(r.nodeid, r.outcome, r.longreprtext) for r in calls] == [
        ("::passes", "passed", ""),
        ("::fails", "failed", "fails failed: 3 == 4"),
        ("::raises", "failed", "raises failed: boom"),
    ]
