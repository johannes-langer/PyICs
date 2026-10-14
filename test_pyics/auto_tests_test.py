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
from pyics.tests.tests import (
    Make,
    MarkupAssertionError,
    PrintTest,
    Raises,
    SimpleTest,
    SpecTest,
)

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


ARRAY_SOLUTION = """
import numpy as np

def double(a):
    return np.asarray(a) * 2

def mean(a):
    return np.mean(a)
"""


@pytest.mark.parametrize("stored", [False, True])
def test_numpy_arrays_are_compared_as_a_whole(tmp_path: Path, stored: bool) -> None:
    """A test expecting an array passes for a correct solution, and fails for a wrong
    one, also when stored in a task file."""
    np = pytest.importorskip("numpy")
    solution = tmp_path / "solution.py"
    solution.write_text(ARRAY_SOLUTION)

    def test(expected: object, function_name: str = "double") -> SimpleTest:
        test = SimpleTest("t", solution, function_name, expected, function_args=[[1, 2]])
        return json.loads(test.serialize()) if stored else test

    test(np.array([2, 4])).run()
    test(np.float64(1.5), "mean").run()
    test(1.5, "mean").run()
    with pytest.raises(AssertionError) as excinfo:
        test(np.array([2, 0])).run()
    assert str(excinfo.value).splitlines() == [
        "array([2, 4]) == array([2, 0])",
        "",
        "Values differ at 1 of 2 positions:",
        "At index 1 diff: 4 != 0",
    ]


# --- SimpleTest: sequences of calls, objects and exceptions ---

STATEFUL = """
class Stack:
    def __init__(self, items=()):
        self.items = list(items)

    @classmethod
    def from_list(cls, items):
        return cls(items)

    def push(self, item):
        self.items.append(item)

    def pop(self):
        return self.items.pop()

    @property
    def size(self):
        return len(self.items)


class Account:
    def __init__(self, balance):
        if balance < 0:
            raise ValueError("negative balance")
        self.balance = balance

    def withdraw(self, amount):
        if amount > self.balance:
            raise InsufficientFunds(amount)
        self.balance -= amount


class InsufficientFunds(Exception):
    pass


account = Account(10)
counter = 0


def count():
    global counter
    counter += 1
    return counter


def add_item(item, items=[]):
    items.append(item)
    return items
"""


@pytest.fixture
def stateful(tmp_path: Path) -> Path:
    path = tmp_path / "stateful.py"
    path.write_text(STATEFUL)
    return path


def _failure(test: SimpleTest) -> list[str]:
    with pytest.raises(AssertionError) as excinfo:
        test.run()
    return str(excinfo.value).splitlines()


def test_stored_single_calls_still_run(stateful: Path) -> None:
    """Tests stored before sequences, objects and isolation have none of their
    attributes, which the class provides."""
    stored = {
        "__instance_type__": ["pyics.tests.tests", "SimpleTest"],
        "attributes": {
            "name": "t",
            "file_path": str(stateful),
            "function_name": "add_item",
            "function_args": [1],
            "function_kwargs": {},
            "expected": [1],
        },
    }
    test = json.loads(json.dumps(stored))
    assert isinstance(test, SimpleTest)
    assert (test.constructor, test.isolated) == (None, False)
    test.run()
    # a single call is stored as before, without the new attributes
    assert "constructor" not in vars(SimpleTest("t", stateful, "count", 1))


def test_sequences_and_objects_survive_a_json_round_trip(stateful: Path) -> None:
    test = SimpleTest(
        "t",
        stateful,
        ["push", "size", "pop", "pop"],
        [None, 1, (2, 3), Raises(IndexError)],
        function_args=[[(2, 3)], [], [], []],
        constructor="Stack",
        constructor_args=[[]],
        isolated=True,
    )
    loaded = json.loads(test.serialize())
    assert isinstance(loaded, SimpleTest)
    assert vars(loaded) == vars(test)
    assert isinstance(loaded.expected[2], tuple)
    assert loaded.expected[3] == Raises("IndexError")
    loaded.run()


def test_calls_run_in_succession_on_the_module(stateful: Path) -> None:
    SimpleTest("t", stateful, ["count", "count", "count"], [1, 2, 3]).run()


def test_the_failing_call_is_shown_after_the_calls_before(stateful: Path) -> None:
    test = SimpleTest("t", stateful, ["count", "count", "count"], [1, 2, 4])
    assert _failure(test)[:4] == [
        "count() -> 1",
        "count() -> 2",
        "count() returned 3, expected 4",
        "3 == 4",
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"function_name": ["count", "count"], "expected": [1]}, "expected must be"),
        (
            {"function_name": ["count"], "expected": [1], "function_args": [[], []]},
            "function_args must be",
        ),
        (
            {"function_name": ["count"], "expected": [1], "function_kwargs": [1]},
            "function_kwargs must be",
        ),
        ({"function_name": ["count", 1], "expected": [1, 2]}, "must be str"),
        ({"function_name": [], "expected": []}, "non-empty list"),
        ({"function_name": "count", "expected": 1, "constructor_args": [1]}, "need a"),
    ],
)
def test_malformed_sequences_are_rejected(
    stateful: Path, kwargs: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SimpleTest("t", stateful, **kwargs)


def test_methods_of_an_object(stateful: Path) -> None:
    SimpleTest(
        "t",
        stateful,
        ["push", "push", "pop", "pop"],
        [None, None, 2, 1],
        function_args=[[1], [2], [], []],
        constructor="Stack",
    ).run()

    wrong = SimpleTest(
        "t",
        stateful,
        ["push", "pop"],
        [None, 2],
        function_args=[[1], []],
        constructor="Stack",
    )
    assert _failure(wrong)[:3] == [
        "Stack()",
        ".push(1) -> None",
        ".pop() returned 1, expected 2",
    ]


def test_dotted_names_and_attributes(stateful: Path) -> None:
    # a module-level object's method, and its attribute compared as a value
    SimpleTest(
        "t",
        stateful,
        ["account.withdraw", "account.balance"],
        [None, 7],
        function_args=[[3], []],
    ).run()
    # a class method as the constructor, and a property compared as a value
    SimpleTest(
        "t", stateful, ["push", "size"], [None, 1], function_args=[[1], []],
        constructor="Stack.from_list", constructor_args=[[]],
    ).run()  # fmt: skip
    size = r"^Stack\(\)\n\.size returned 0, expected 1"
    with pytest.raises(AssertionError, match=size):
        SimpleTest("t", stateful, "size", 1, constructor="Stack").run()
    with pytest.raises(AssertionError, match="balance is not callable"):
        SimpleTest(
            "t", stateful, ["account.balance", "count"], [10, 1],
            function_args=[[1], []],
        ).run()  # fmt: skip


def test_a_function_that_keeps_state_gets_a_hint(stateful: Path) -> None:
    test = SimpleTest(
        "t", stateful, ["add_item", "add_item"], [[1], [1]], function_args=[[1], [1]]
    )
    lines = _failure(test)
    assert lines[:2] == [
        "add_item(1) -> [1]",
        "add_item(1) returned [1, 1], expected [1]",
    ]
    assert "the function keeps state between calls" in lines[-1]

    # different calls get no hint
    test = SimpleTest(
        "t", stateful, ["add_item", "add_item"], [[1], [2]], function_args=[[1], [2]]
    )
    assert "keeps state" not in "\n".join(_failure(test))


def test_expected_exceptions(stateful: Path) -> None:
    # a builtin exception, and one the exercise file defines
    SimpleTest("t", stateful, ["pop"], [Raises(IndexError)], constructor="Stack").run()
    SimpleTest(
        "t", stateful, ["withdraw", "balance"], [Raises("InsufficientFunds"), 1],
        function_args=[[2], []], constructor="Account", constructor_args=[1],
    ).run()  # fmt: skip
    # a subclass counts, too
    SimpleTest("t", stateful, ["pop"], [Raises(LookupError)], constructor="Stack").run()


def test_a_constructor_that_must_raise(stateful: Path) -> None:
    SimpleTest("t", stateful, "Account", Raises(ValueError), function_args=[-1]).run()
    with pytest.raises(
        AssertionError,
        match=r"^expected Account\(1\) to raise ValueError, but it returned <",
    ):
        SimpleTest(
            "t", stateful, "Account", Raises(ValueError), function_args=[1]
        ).run()


def test_wrong_or_missing_exceptions_fail(stateful: Path) -> None:
    wrong = SimpleTest("t", stateful, "pop", Raises(KeyError), constructor="Stack")
    assert _failure(wrong) == [
        "Stack()",
        "expected .pop() to raise KeyError, but it raised IndexError: pop from empty "
        "list",
    ]
    missing = SimpleTest("t", stateful, "count", Raises("ValueError"))
    assert _failure(missing) == [
        "expected count() to raise ValueError, but it returned 1"
    ]
    unknown = SimpleTest("t", stateful, "count", Raises("Undefined"))
    assert "Undefined is not an exception class" in _failure(unknown)[0]


GRAPHS = """
class Graph:
    def __init__(self, edges, *, name=""):
        if edges is None:
            raise ValueError("no edges")
        self.edges = list(edges)
        self.name = name

    def __eq__(self, other):
        return isinstance(other, Graph) and other.edges == self.edges

    def successors(self, node):
        return [b for a, b in self.edges if a == node]

def degree(g, node):
    return (type(g).__module__, len(g.successors(node)))

def degrees(graphs, node):
    return [len(g.successors(node)) for g in graphs]

def union(g, other=None):
    return Graph(g.edges + other.edges)

class Holder:
    def __init__(self, g):
        self.g = g

    def size(self):
        return len(self.g.edges)
"""

EDGES = [("A", "B"), ("A", "C"), ("B", "C")]


@pytest.fixture
def graphs(tmp_path: Path) -> Path:
    path = tmp_path / "graphs.py"
    path.write_text(GRAPHS)
    return path


def test_make_creates_objects_of_the_exercise_file(graphs: Path) -> None:
    test = SimpleTest(
        "t", graphs, "degree", ("current_solution", 2),
        function_args=[Make("Graph", EDGES), "A"],
    )  # fmt: skip
    loaded = json.loads(json.dumps(test))
    assert loaded.function_args[0] == Make("Graph", EDGES)
    loaded.run()


def test_make_in_lists_kwargs_constructors_and_expected_values(graphs: Path) -> None:
    graph = Make("Graph", EDGES, name="g")
    SimpleTest(
        "t", graphs, "degrees", [2, 0], function_args=[[graph, Make("Graph", [])], "A"]
    ).run()
    SimpleTest(
        "t", graphs, "union", Make("Graph", EDGES + EDGES),
        function_args=[graph], function_kwargs={"other": graph},
    ).run()  # fmt: skip
    SimpleTest(
        "t", graphs, "size", 3, constructor="Holder", constructor_args=[graph]
    ).run()
    wrong = SimpleTest(
        "t", graphs, "union", Make("Graph", []), function_args=[graph],
        function_kwargs={"other": graph},
    )  # fmt: skip
    assert "current_solution.Graph object" in _failure(wrong)[0]


def test_make_failures_name_the_call(graphs: Path) -> None:
    missing = SimpleTest(
        "t", graphs, "degree", Raises(AttributeError),
        function_args=[Make("Tree", EDGES), "A"],
    )  # fmt: skip
    (line,) = _failure(missing)
    assert line.startswith("creating the values of degree(Make('Tree', ")
    assert line.endswith(
        "raised AttributeError: module 'current_solution' has no attribute 'Tree'"
    )
    raising = SimpleTest(
        "t", graphs, "degree", 0, function_args=[Make("Graph", None), "A"]
    )
    assert "raised ValueError: no edges" in _failure(raising)[0]
    constructor = SimpleTest(
        "t", graphs, "size", 0, constructor="Holder",
        constructor_args=[Make("Graph", None)],
    )  # fmt: skip
    assert "creating the object" in _failure(constructor)[0]


def test_make_marker() -> None:
    assert repr(Make("Graph", [("A", "B")], name="g")) == (
        "Make('Graph', [('A', 'B')], name='g')"
    )
    assert Make("a.B", 1) == Make("a.B", 1) != Make("a.B", 2)
    with pytest.raises(TypeError):
        Make("not a name")


def test_raises_marker() -> None:
    assert Raises(ValueError) == Raises("ValueError")
    assert repr(Raises(ValueError)) == "Raises('ValueError')"
    assert Raises("a.B") != Raises("B")
    with pytest.raises(TypeError):
        Raises(int)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        Raises("not a name")


def test_unexpected_exceptions_name_the_call(stateful: Path) -> None:
    test = SimpleTest(
        "t",
        stateful,
        ["push", "pop", "pop", "push"],
        [None, 1, 0, None],
        function_args=[[1], [], [], [2]],
        constructor="Stack",
    )
    with pytest.raises(AssertionError) as excinfo:
        test.run()
    assert str(excinfo.value).splitlines() == [
        "Stack()",
        ".push(1) -> None",
        ".pop() -> 1",
        "call 3 of 4 (.pop()) raised IndexError: pop from empty list",
    ]
    assert isinstance(excinfo.value.__cause__, IndexError)

    broken = SimpleTest("t", stateful, "pop", 1, constructor="Account")
    with pytest.raises(AssertionError, match=r"^creating the object, Account\(\)"):
        broken.run()


ISOLATED = """
from pathlib import Path

def write():
    Path("out.txt").write_text("written")
    return sorted(p.name for p in Path().iterdir())

if __name__ == "__main__":
    print(write())
"""


def test_isolated_tests_leave_no_files_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    (tmp_path / "ex").mkdir()
    path = tmp_path / "ex" / "isolated.py"
    path.write_text(ISOLATED)
    # relative to the working directory, which the tests leave
    relative = Path("..", "ex", "isolated.py")

    simple = SimpleTest("t", relative, "write", ["out.txt"], isolated=True)
    simple.run()
    PrintTest("t", relative, "['out.txt']", isolated=True).run()
    assert simple.file_path == relative  # restored
    assert list(work.iterdir()) == []
    assert Path.cwd() == work

    # without isolation, the file is written to the working directory
    SimpleTest("t", relative, "write", ["out.txt"]).run()
    assert [p.name for p in work.iterdir()] == ["out.txt"]


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
