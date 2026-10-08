"""
Test whether the automatic tests work.
"""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import pyics.util.json_handling as json
from pyics.data import Task
from pyics.tests.pytest_integration import LiveTestCollectionPlugin
from pyics.tests.tests import MarkupAssertionError, PrintTest, SimpleTest, SpecTest

SOLUTION = """
def add(a, b=0):
    return a + b

def neighbours(edges, node):
    return [b for a, b in edges if a == node]

def path(start, goal):
    return (start, goal)

def fail():
    raise ValueError("boom")

def append_four(xs):
    xs.append(4)
    return xs
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


def test_edit_keeping_size_and_second_is_picked_up(solution: Path) -> None:
    """E.g. a typo fixed and saved right after the previous save."""
    mtime = solution.stat().st_mtime
    test = SimpleTest("t", solution, "add", 3, function_args=[1, 2])
    test.run()

    solution.write_text(SOLUTION.replace("a + b", "a * b"))
    os.utime(solution, (mtime, mtime))
    with pytest.raises(AssertionError, match=r"^2 == 3$"):
        test.run()


@pytest.mark.parametrize("by_keyword", [False, True])
def test_solution_cannot_change_the_arguments(solution: Path, by_keyword: bool) -> None:
    xs = [1, 2, 3]
    test = SimpleTest(
        "t",
        solution,
        "append_four",
        [1, 2, 3, 4],
        function_args=[] if by_keyword else [xs],
        function_kwargs={"xs": xs} if by_keyword else {},
    )
    test.run()
    test.run()  # e.g. after the exercise file is saved again
    assert xs == [1, 2, 3]


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


# --- PrintTest ---

SCRIPT = """
def greet(name):
    print(f"Hello, {name}!")

if __name__ == "__main__":
    greet("World")
    print("Bye")
"""


def _script(tmp_path: Path, code: str) -> Path:
    path = tmp_path / "script.py"
    path.write_text(code)
    return path


@pytest.fixture
def script(tmp_path: Path) -> Path:
    return _script(tmp_path, SCRIPT)


def test_print_matching_output_passes(
    script: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    PrintTest("t", script, "Hello, World!\nBye\n").run()
    assert capsys.readouterr().out == ""  # captured, not shown


def test_print_runs_the_file_as_a_script(tmp_path: Path) -> None:
    PrintTest("t", _script(tmp_path, "print(__name__)"), "__main__").run()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('print("Hello")', "Hello"),  # no line break at the end
        ('print("Hello")\nprint()\nprint(" ")', "Hello"),  # blank lines at the end
        ('for i in range(3):\n    print(i, end=" ")', "0 1 2"),  # spaces at line ends
        ('print("Hello")', "Hello \r\n\n"),
    ],
)
def test_print_invisible_whitespace_is_ignored(
    tmp_path: Path, code: str, expected: str
) -> None:
    PrintTest("t", _script(tmp_path, code), expected).run()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('print("Hello")', " Hello"),  # indentation
        ('print("Hello")\nprint("Bye")', "Hello\n\nBye"),  # blank lines in between
        ('print("Hello", "World")', "Hello  World"),  # spaces within lines
        ('print("Hello", end="")\nprint("Bye")', "Hello\nBye"),  # line breaks
    ],
)
def test_print_visible_whitespace_matters(
    tmp_path: Path, code: str, expected: str
) -> None:
    with pytest.raises(AssertionError):
        PrintTest("t", _script(tmp_path, code), expected).run()


def test_print_failure_explains_the_difference(script: Path) -> None:
    test = PrintTest("t", script, "Hello, World\nBye")
    with pytest.raises(AssertionError) as excinfo:
        test.run()
    lines = str(excinfo.value).splitlines()
    assert lines[0] == r"'Hello, World!\nBye' == 'Hello, World\nBye'"
    assert "- Hello, World" in lines  # expected
    assert "+ Hello, World!" in lines  # printed


@pytest.mark.parametrize("exit_call", ["sys.exit()", "exit()"])
def test_print_exiting_ends_the_program(tmp_path: Path, exit_call: str) -> None:
    code = f'import sys\nprint("Hello")\n{exit_call}\nprint("Bye")'
    PrintTest("t", _script(tmp_path, code), "Hello").run()


def test_print_errors_in_the_file_propagate(tmp_path: Path) -> None:
    code = 'print("Hello")\nraise ValueError("boom")'
    with pytest.raises(ValueError, match="boom"):
        PrintTest("t", _script(tmp_path, code), "Hello").run()


INTERACTIVE_SCRIPT = """
name = input("Name: ")
age = int(input("Age: "))
print(f"{name} is {age}.")
"""


def test_print_inputs_answer_input_calls_in_order(tmp_path: Path) -> None:
    # the answers are shown after their prompts, as if typed into the console
    expected = "Name: Bob\nAge: 42\nBob is 42."
    test = PrintTest(
        "t", _script(tmp_path, INTERACTIVE_SCRIPT), expected, inputs=["Bob", "42"]
    )
    test.run()


@pytest.mark.parametrize("inputs", [[], ["Bob"]])
def test_print_running_out_of_inputs_fails(tmp_path: Path, inputs: list[str]) -> None:
    """Rather than waiting for input from the console."""
    test = PrintTest("t", _script(tmp_path, INTERACTIVE_SCRIPT), "", inputs=inputs)
    with pytest.raises(EOFError):
        test.run()


def test_print_inputs_must_be_a_list(script: Path) -> None:
    with pytest.raises(TypeError, match="list"):
        PrintTest("t", script, "", inputs="Bob")  # pyright: ignore[reportArgumentType]


def test_print_captures_only_its_own_thread(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. the CLI keeps drawing while a task's tests run in the background."""
    sync = SimpleNamespace(printed=threading.Event(), resume=threading.Event())
    monkeypatch.setitem(sys.modules, "sync", sync)  # imported by the script
    code = """
import sync
print("Hello")
sync.printed.set()
sync.resume.wait(5)
print("Bye")
"""
    test = PrintTest("t", _script(tmp_path, code), "Hello\nBye")

    with ThreadPoolExecutor() as pool:
        run = pool.submit(test.run)
        assert sync.printed.wait(5)
        print("drawn meanwhile")
        sync.resume.set()
        run.result(timeout=5)  # raises if the test failed

    assert capsys.readouterr().out == "drawn meanwhile\n"


def test_print_json_round_trip(tmp_path: Path) -> None:
    test = PrintTest(
        "t",
        _script(tmp_path, INTERACTIVE_SCRIPT),
        "Name: Bob\nAge: 42\nBob is 42.",
        inputs=["Bob", "42"],
    )
    loaded = json.loads(test.serialize())
    assert isinstance(loaded, PrintTest)
    assert vars(loaded) == vars(test)
    loaded.run()


# --- running tests through pytest ---


def test_run_through_pytest(
    solution: Path, script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run tests like a ``Task`` does, collecting their reports."""
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    plugin = LiveTestCollectionPlugin(
        [
            SimpleTest("passes", solution, "add", 3, function_args=[1, 2]),
            SimpleTest("fails", solution, "add", 4, function_args=[1, 2]),
            SimpleTest("raises", solution, "fail", None),
            PrintTest("prints", script, "Hello, World!\nBye"),
        ]
    )
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])

    calls = [r for r in plugin.reports if r.when == "call"]
    assert [(r.nodeid, r.outcome, r.longreprtext) for r in calls] == [
        ("::passes", "passed", ""),
        ("::fails", "failed", "fails failed: 3 == 4"),
        ("::raises", "failed", "raises failed: boom"),
        ("::prints", "passed", ""),
    ]



def test_failures_are_highlighted_in_the_task_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    ex = tmp_path / "ex.py"
    ex.write_text("def f(xs: list[int]) -> str:\n    return '[b]' + str(xs)\n")
    tests = [
        SpecTest("spec [b]check[/]", ex, "f", {"is_class": False, "xs": "list[str]"}),
        SimpleTest("simple", ex, "f", "[b]x[/]", function_args=[[]]),
    ]
    task_file = Task.create_task_json(tmp_path / "t", "t", "", ex, ex, tests)
    task = Task(task_file, setup=False)
    assert not task.check_solved()

    segments = list(Console(width=200).render(task.status))
    text = "".join(s.text for s in segments)
    # the markup of the spec check is rendered, everything else is shown as it is
    assert (
        "spec [b]check[/] failed: Parameter 'xs' type mismatch. "
        "Expected list[str], got list[int]."
    ) in text
    assert "simple failed: '[b][]' == '[b]x[/]'" in text
    bold = {s.text for s in segments if s.style and s.style.bold}
    assert {"'xs'", "list[str]", "list[int]"} <= bold


def test_malformed_markup_is_shown_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Malformed(SimpleTest):
        def run(self) -> None:
            raise MarkupAssertionError("[/] closes nothing")

    monkeypatch.chdir(tmp_path)
    plugin = LiveTestCollectionPlugin([Malformed("t", tmp_path, "f", None)])
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])

    [report] = [r for r in plugin.reports if r.when == "call"]
    assert report.longreprtext == "t failed: [/] closes nothing"
