"""
Drive the interactive test-creation flow of ``create_task`` with scripted key presses.
"""

import io
import queue
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from notebook_helpers import markdown, write_notebook
from rich.console import Console

from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.scripts import create_task
from pyics.scripts.create_task import (
    _INFO_MODULE,
    _TEST_TYPES,
    _check_file,
    _load_namespace,
    _prompt_files,
    _prompt_test,
    _prompt_tests,
)
from pyics.tests.tests import (
    GeneralTest,
    Make,
    PrintTest,
    Raises,
    SimpleTest,
    SpecTest,
    StructureTest,
)

DOWN, ENTER, BACKSPACE, CTRL_D = "DOWN", "\r", "\x7f", "\x04"

INFO_FILE = """
edges = [("A", "B", 4), ("B", "C", 5)]
all_edges = edges + [(b, a, c) for a, b, c in edges]

name = "Search path test"
function_name = "depth_first_search"

exp = ("A", "B", "C")
args = [all_edges, "A", "C"]
"""

SOLUTION_FILE = """
def depth_first_search(edges: list[tuple[str, str, int]], start: str, goal: str) -> bool:
    return True

symbol_name = "depth_first_search"
"""

SOLUTION_SPEC = {
    "is_class": False,
    "edges": "list[tuple[str, str, int]]",
    "start": "str",
    "goal": "str",
    "return": "bool",
}


def _run_flow[T](keys: list[str], flow: Callable[[PromptSession], T]) -> T:
    q: queue.Queue[str] = queue.Queue()
    # a trailing ctrl-c cancels instead of hanging if the flow asks for more than scripted
    for key in [*keys, "\x03"]:
        q.put(key)
    result = PromptSession(MagicMock(), q).run(flow)
    assert q.get_nowait() == "\x03", "flow finished before all scripted keys were used"
    return result


def _run(keys: list[str], ex_files: list[Path]) -> SimpleTest:
    test = _run_flow(keys, lambda s: _prompt_test(s, SimpleTest, ex_files))
    assert isinstance(test, SimpleTest)
    return test


def _type(text: str) -> list[str]:
    return [*text, ENTER]


@pytest.fixture
def ex_files(tmp_path: Path) -> list[Path]:
    ex = tmp_path / "ex.py"
    ex.touch()
    return [ex]


def test_symbols_from_info_file(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text(INFO_FILE)
    test = _run(
        [
            ENTER,  # reference a file: yes
            *_type(str(info)),
            # name, function_name: exact matches
            ENTER,  # file_path: the only exercise file
            ENTER,  # call functions of the module
            BACKSPACE, BACKSPACE, *_type("args[1:]"),  # function_args, prefilled with []
            ENTER,  # function_kwargs: keep {}
            *_type("exp"),  # expected
            ENTER,  # add another call: no
            ENTER, ENTER, ENTER,  # traceable, isolated, soft: keep True, False, False
        ],
        ex_files,
    )  # fmt: skip
    assert test.name == "Search path test"
    assert test.function_name == "depth_first_search"
    assert test.file_path == ex_files[0].absolute()
    assert test.expected == ("A", "B", "C")
    assert test.function_args == ["A", "C"]
    assert test.function_kwargs == {}
    assert test.traceable is True
    assert test.isolated is False


def test_missing_info_file_is_asked_again(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text(INFO_FILE)
    test = _run(
        [
            ENTER,  # reference a file: yes
            *_type(str(tmp_path / "info")),  # no such file...
            *_type(".py"),  # ...fixed, keeping the rejected input
            ENTER, ENTER,  # file_path, call functions of the module
            ENTER, ENTER, *_type("exp"),  # function_args, function_kwargs, expected
            ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft
        ],
        ex_files,
    )  # fmt: skip
    assert test.name == "Search path test"


def test_missing_file_is_asked_again(tmp_path: Path) -> None:
    ex = tmp_path / "ex.py"
    ex.touch()
    files = _run_flow(
        [
            *_type(str(tmp_path)),  # a directory...
            *_type("/ex"),  # ...no such file...
            *_type(".py"),  # ...fixed, keeping the rejected input
            DOWN, ENTER,  # add another exercise file: no
        ],
        lambda s: _prompt_files(s, "exercise"),
    )  # fmt: skip
    assert files == [ex]


def test_check_file_expands_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # read instead on Windows
    (tmp_path / "ex.py").touch()
    assert _check_file("~/ex.py") == tmp_path / "ex.py"


def test_invalid_expression_is_asked_again(ex_files: list[Path]) -> None:
    test = _run(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"),  # name
            ENTER,  # file_path
            ENTER,  # call functions of the module
            *_type("f"),  # function_name
            ENTER, ENTER,  # function_args, function_kwargs: defaults
            *_type("1 +"),  # expected: syntax error...
            *_type(" 1"),  # ...fixed, keeping the rejected input
            ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft: defaults
        ],
        ex_files,
    )  # fmt: skip
    assert test.expected == 2


def test_unusable_match_is_prompted_for(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text("name = 5\nfunction_name = 'f'\nexpected = 1\n")
    test = _run(
        [
            ENTER,
            *_type(str(info)),
            # name isn't a str: enter text instead of picking the str 'function_name'
            DOWN, ENTER, *_type("n"),
            ENTER, ENTER,  # file_path, call functions of the module
            # function_name, expected: exact matches
            ENTER, ENTER,  # function_args, function_kwargs: defaults
            ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft: defaults
        ],
        ex_files,
    )  # fmt: skip
    assert test.name == "n"


def test_without_info_file(ex_files: list[Path]) -> None:
    test = _run(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"),  # name
            ENTER,  # file_path
            ENTER,  # call functions of the module
            *_type("f"),  # function_name
            ENTER, ENTER,  # function_args, function_kwargs: defaults
            *_type("1"),  # expected
            ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft: defaults
        ],
        ex_files,
    )  # fmt: skip
    assert (test.name, test.function_name, test.expected) == ("t", "f", 1)
    assert test.function_args == []
    assert test.function_kwargs == {}


def test_traceable_must_be_a_bool(ex_files: list[Path]) -> None:
    test = _run(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"), ENTER, ENTER,  # name, file_path, call functions
            *_type("f"),  # function_name
            ENTER, ENTER,  # function_args, function_kwargs: keep defaults
            *_type("1"),  # expected
            ENTER,  # add another call: no
            *[BACKSPACE] * len("True"), *_type("1"),  # traceable: not a bool...
            BACKSPACE, *_type("False"),  # ...fixed
            ENTER, ENTER,  # isolated, soft: keep False
        ],
        ex_files,
    )  # fmt: skip
    assert test.traceable is False


def test_a_test_can_be_made_soft(ex_files: list[Path]) -> None:
    test = _run(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"), ENTER, ENTER,  # name, file_path, call functions
            *_type("f"), ENTER, ENTER, *_type("1"),  # one call
            ENTER, ENTER, ENTER,  # add another call: no; traceable, isolated: keep
            *[BACKSPACE] * len("False"), *_type("True"),  # soft
        ],
        ex_files,
    )  # fmt: skip
    assert test.soft is True

    structure = _run_flow(
        [
            DOWN, ENTER, *_type("t"), ENTER,  # no info file, name, file_path
            *_type('{"concept": "loop", "max": 0}'),  # rules
            *[BACKSPACE] * len("False"), *_type("True"),  # soft
        ],
        lambda s: _prompt_test(s, StructureTest, ex_files),
    )  # fmt: skip
    assert isinstance(structure, StructureTest) and structure.soft is True


def test_too_few_keys_cancels(ex_files: list[Path]) -> None:
    with pytest.raises(PromptCancelled):
        _run([DOWN, ENTER], ex_files)


def _run_spec_test(keys: list[str], ex_files: list[Path]) -> SpecTest:
    test = _run_flow(keys, lambda s: _prompt_test(s, SpecTest, ex_files))
    assert isinstance(test, SpecTest)
    return test


def test_spec_from_function_in_scope(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "solution.py"
    info.write_text(SOLUTION_FILE)
    test = _run_spec_test(
        [
            ENTER, *_type(str(info)),  # reference the solution file
            *_type("t"),  # name
            ENTER,  # file_path; symbol_name: exact match
            *_type("depth_first_search"),  # expected: the function itself
            ENTER, ENTER,  # by_type, soft: keep False
        ],
        ex_files,
    )  # fmt: skip
    assert test.symbol_name == "depth_first_search"
    assert test.expected == SOLUTION_SPEC


def test_spec_from_matching_function(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "solution.py"
    info.write_text(SOLUTION_FILE + "expected = depth_first_search\n")
    test = _run_spec_test(
        [
            ENTER, *_type(str(info)),
            *_type("t"),  # name
            ENTER,  # file_path; symbol_name, expected: exact matches
            ENTER, ENTER,  # by_type, soft: keep False
        ],
        ex_files,
    )  # fmt: skip
    assert test.expected == SOLUTION_SPEC


def test_malformed_spec_is_asked_again(ex_files: list[Path]) -> None:
    test = _run_spec_test(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"),  # name
            ENTER,  # file_path
            *_type("f"),  # symbol_name
            *_type("{}"),  # expected: no 'is_class' key...
            BACKSPACE, *_type('"is_class": False}'),  # ...fixed
            ENTER, ENTER,  # by_type, soft: keep False
        ],
        ex_files,
    )  # fmt: skip
    assert test.expected == {"is_class": False}


def _run_print_test(keys: list[str], ex_files: list[Path]) -> PrintTest:
    test = _run_flow(keys, lambda s: _prompt_test(s, PrintTest, ex_files))
    assert isinstance(test, PrintTest)
    return test


def test_print_test_expected_output_spans_lines(ex_files: list[Path]) -> None:
    test = _run_print_test(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"),  # name
            ENTER,  # file_path
            *"Name: Bob", ENTER, *"Hello, Bob!", CTRL_D,  # expected: enter adds a line
            BACKSPACE, BACKSPACE, *_type('["Bob"]'),  # inputs, prefilled with []
            ENTER, ENTER, ENTER,  # traceable, isolated, soft: keep True, False, False
        ],
        ex_files,
    )  # fmt: skip
    assert test.expected == "Name: Bob\nHello, Bob!"
    assert test.inputs == ["Bob"]


def test_print_test_from_info_file(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text(
        'name = "greeting"\nexpected = "Name: Bob\\nHello, Bob!\\n"\ninputs = ["Bob"]\n'
    )
    test = _run_print_test(
        [
            ENTER, *_type(str(info)),  # reference the info file
            ENTER,  # file_path; name, expected, inputs: exact matches
            ENTER, ENTER, ENTER,  # traceable, isolated, soft: keep True, False, False
        ],
        ex_files,
    )  # fmt: skip
    assert (test.name, test.expected) == ("greeting", "Name: Bob\nHello, Bob!\n")
    assert test.inputs == ["Bob"]


@pytest.mark.parametrize("rejected", ['"Bob"', "[42]"])
def test_print_test_inputs_must_be_strings_in_a_list(
    ex_files: list[Path], rejected: str
) -> None:
    test = _run_print_test(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"),  # name
            ENTER,  # file_path
            CTRL_D,  # expected: no output
            BACKSPACE, BACKSPACE, *_type(rejected),  # inputs...
            *[BACKSPACE] * len(rejected), *_type('["Bob"]'),  # ...fixed
            ENTER, ENTER, ENTER,  # traceable, isolated, soft: keep True, False, False
        ],
        ex_files,
    )  # fmt: skip
    assert (test.expected, test.inputs) == ("", ["Bob"])


def test_print_test_is_offered(ex_files: list[Path]) -> None:
    tests = _run_flow(
        [
            *[DOWN] * _TEST_TYPES.index(PrintTest), ENTER,  # PrintTest
            DOWN, ENTER, *_type("t"), ENTER,  # no info file, name, file_path
            CTRL_D, ENTER, ENTER, ENTER, ENTER,  # no output; keep the rest
            *[DOWN] * len(_TEST_TYPES), ENTER,  # done
        ],
        lambda s: _prompt_tests(s, ex_files),
    )  # fmt: skip
    assert [type(test) for test in tests] == [PrintTest]


def test_malformed_structure_rules_are_asked_again(ex_files: list[Path]) -> None:
    test = _run_flow(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("t"),  # name
            ENTER,  # file_path
            *_type('{"concept": "loop", "max": -1}'),  # rules: negative max...
            *[BACKSPACE] * 3, *_type("0}"),  # ...fixed
            ENTER,  # soft: keep False
        ],
        lambda s: _prompt_test(s, StructureTest, ex_files),
    )  # fmt: skip
    assert isinstance(test, StructureTest)
    assert test.rules == [{"concept": "loop", "max": 0}]


# --- notebooks as test information files ---

SOLUTION_NOTEBOOK = [
    markdown("# Depth-first search"),
    "%matplotlib inline\nimport math",
    SOLUTION_FILE,
    'print("not shown")\nexpected = depth_first_search',
    "unfinished",  # cell 5 raises a NameError
    "display(math.pi)\nexpected",
]
CELL_5_RAISED = (
    "In the test information file, cell 5 raised NameError: name 'unfinished' is not "
    "defined"
)


def test_notebooks_are_run_for_their_variables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    info = write_notebook(tmp_path / "solution.ipynb", SOLUTION_NOTEBOOK)
    symbols, problem = _load_namespace(str(info))
    # neither modules, nor the stand-ins of the notebook's get_ipython() and display()
    assert set(symbols) == {"depth_first_search", "symbol_name", "expected"}
    assert symbols["expected"] is symbols["depth_first_search"]
    assert problem == CELL_5_RAISED
    assert capsys.readouterr().out == ""
    assert _INFO_MODULE not in sys.modules

    py = tmp_path / "solution.py"
    py.write_text(SOLUTION_FILE)
    symbols, problem = _load_namespace(str(py))
    assert set(symbols) == {"depth_first_search", "symbol_name"}
    assert problem == ""


def test_spec_from_a_solution_notebook(tmp_path: Path, ex_files: list[Path]) -> None:
    info = write_notebook(tmp_path / "solution.ipynb", SOLUTION_NOTEBOOK)
    q: queue.Queue[str] = queue.Queue()
    keys = [
        ENTER, *_type(str(info)),  # reference the solution notebook
        DOWN, ENTER, *_type("t"),  # name: enter text
        ENTER,  # file_path; symbol_name, expected: exact matches
        ENTER, ENTER,  # by_type, soft: keep False
    ]  # fmt: skip
    for key in [*keys, "\x03"]:
        q.put(key)
    cli = MagicMock()
    test = PromptSession(cli, q).run(lambda s: _prompt_test(s, SpecTest, ex_files))
    assert q.get_nowait() == "\x03", "all keys were used"
    assert isinstance(test, SpecTest)
    assert (test.name, test.symbol_name) == ("t", "depth_first_search")
    assert test.expected == SOLUTION_SPEC

    # the prompts for what the notebook doesn't define say that a cell raised
    calls = cli.update_main_content.call_args_list
    by_type = next(c.args[0] for c in reversed(calls) if c.kwargs["title"] == "by_type")
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(by_type)
    assert CELL_5_RAISED in console.export_text()


# --- SimpleTests of objects, and sequences of calls ---

UP, ESC = "UP", "\x1b"


def _clear(text: str) -> list[str]:
    return [BACKSPACE] * len(text)


def test_method_sequence(ex_files: list[Path]) -> None:
    test = _run(
        [
            DOWN, ENTER,  # reference a file: no
            *_type("stack"),  # name
            ENTER,  # file_path
            DOWN, ENTER,  # create an object and call its methods
            *_type("Stack"),  # constructor
            ENTER, ENTER,  # constructor_args, constructor_kwargs: none
            *_type("push"), *_clear("[]"), *_type("[1]"), ENTER, *_type("None"),
            UP, ENTER,  # add another call: yes
            ENTER, ENTER, ENTER, ENTER,  # the same call again, prefilled
            UP, ENTER,  # add another call: yes
            *_clear("push"), *_type("pop"), *_clear("[1]"), *_type("[]"), ENTER,
            *_clear("None"), *_type("1"),  # expected
            ENTER,  # add another call: no
            ENTER,  # traceable: keep True
            *_clear("False"), *_type("True"),  # isolated
            ENTER,  # soft: keep False
        ],
        ex_files,
    )  # fmt: skip
    assert test.constructor == "Stack"
    assert (test.constructor_args, test.constructor_kwargs) == ([], {})
    assert test.function_name == ["push", "push", "pop"]
    assert test.function_args == [[1], [1], []]
    assert test.function_kwargs == [{}, {}, {}]
    assert test.expected == [None, None, 1]
    assert test.isolated is True


def test_edit_method_sequence(ex_files: list[Path]) -> None:
    current = SimpleTest(
        "stack",
        ex_files[0],
        ["push", "push", "pop"],
        [None, None, 1],
        function_args=[[1], [1], []],
        constructor="Stack",
        isolated=True,
    )
    cli = MagicMock()
    q: queue.Queue[str] = queue.Queue()
    keys = [
        ENTER,  # reference a file with test information: no, when editing
        ENTER, ENTER, ENTER,  # name, file_path, object: keep
        ENTER, ENTER, ENTER,  # constructor and its arguments: keep
        ENTER, ENTER, ENTER, ENTER, ENTER,  # call 1, another call: yes, preselected
        ENTER, ENTER, ENTER, ENTER, ENTER,  # call 2, another call: yes, preselected
        ENTER, ENTER, ENTER, *_clear("1"), *_type("2"),  # call 3: expect 2
        UP, ENTER,  # add another call: yes, prefilled with call 3
        ENTER, ENTER, ENTER, *_clear("2"), *_type("Raises(IndexError)"),
        ENTER,  # add another call: no
        ENTER, ENTER, ENTER,  # traceable, isolated, soft: keep
    ]  # fmt: skip
    for key in [*keys, "\x03"]:
        q.put(key)
    test = PromptSession(cli, q).run(
        lambda s: _prompt_test(s, SimpleTest, ex_files, current=current)
    )
    assert q.get_nowait() == "\x03", "all keys were used"
    assert isinstance(test, SimpleTest)
    assert test.function_name == ["push", "push", "pop", "pop"]
    assert test.expected == [None, None, 2, Raises("IndexError")]
    assert (test.constructor, test.isolated) == ("Stack", True)

    # the expected exception is named in the details of the prompt
    calls = cli.update_main_content.call_args_list
    title = "call 4: expected"
    prompt = next(c.args[0] for c in reversed(calls) if c.kwargs["title"] == title)
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(prompt)
    assert "Raises(ValueError) expects the call to raise" in console.export_text()


def test_a_single_call_is_stored_as_before(ex_files: list[Path]) -> None:
    test = _run(
        [
            DOWN, ENTER, *_type("t"), ENTER, ENTER,  # name, file_path, module
            *_type("f"), ENTER, ENTER, *_type("Raises(ValueError)"),  # one call
            ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft
        ],
        ex_files,
    )  # fmt: skip
    assert (test.function_name, test.expected) == ("f", Raises("ValueError"))
    assert "isolated" not in vars(test)


def test_constructors_are_offered_from_the_info_file(
    tmp_path: Path, ex_files: list[Path]
) -> None:
    info = tmp_path / "info.py"
    info.write_text("class Stack:\n    pass\n\ndef helper():\n    pass\n")
    test = _run(
        [
            ENTER, *_type(str(info)),  # reference the info file
            *_type("t"), ENTER, DOWN, ENTER,  # name, file_path, object
            ENTER,  # constructor: Stack, the first class or function
            ENTER, ENTER,  # constructor_args, constructor_kwargs
            *_type("pop"), ENTER, ENTER, *_type("None"),  # one call
            ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft
        ],
        ex_files,
    )  # fmt: skip
    assert test.constructor == "Stack"


GRAPH_INFO = """
class Graph:
    def __init__(self, edges):
        self.edges = edges

edges = [("A", "B")]
g = Graph(edges)
"""


def _texts(cli: MagicMock, method: str) -> str:
    """Everything ``cli.<method>`` was called with, as plain text."""
    console = Console(width=300, record=True, file=io.StringIO())
    for call in getattr(cli, method).call_args_list:
        console.print(call.args[0])
    return console.export_text()


def test_objects_of_the_info_file_are_rejected_for_make(
    tmp_path: Path, ex_files: list[Path]
) -> None:
    info = tmp_path / "info.py"
    info.write_text(GRAPH_INFO)
    cli = MagicMock()
    q: queue.Queue[str] = queue.Queue()
    keys = [
        ENTER, *_type(str(info)),  # reference the info file
        *_type("t"), ENTER, ENTER, *_type("degree"),  # name, file_path, module, name
        *_clear("[]"), *_type('[g, "A"]'),  # function_args: rejected, kept
        *_clear('[g, "A"]'), *_type('[Make("Graph", edges), "A"]'),
        ENTER, *_type("1"),  # function_kwargs, expected
        ENTER, ENTER, ENTER, ENTER,  # one call, traceable, isolated, soft
    ]  # fmt: skip
    for key in [*keys, "\x03"]:
        q.put(key)
    test = PromptSession(cli, q).run(lambda s: _prompt_test(s, SimpleTest, ex_files))
    assert q.get_nowait() == "\x03", "all keys were used"
    assert isinstance(test, SimpleTest)
    assert test.function_args == [Make("Graph", [("A", "B")]), "A"]
    info_text = " ".join(_texts(cli, "update_info").split())
    assert "holds a Graph of the test information file" in info_text
    assert "use Make('Graph', ...) instead" in info_text


def test_make_values_are_prefilled_as_code(ex_files: list[Path]) -> None:
    current = SimpleTest(
        "t", ex_files[0], "degree", 1, function_args=[Make("Graph", [("A", "B")]), "A"]
    )
    cli = MagicMock()
    q: queue.Queue[str] = queue.Queue()
    keys = [ENTER] * 12  # no info file, then keep everything
    for key in [*keys, "\x03"]:
        q.put(key)
    test = PromptSession(cli, q).run(
        lambda s: _prompt_test(s, SimpleTest, ex_files, current=current)
    )
    assert q.get_nowait() == "\x03", "all keys were used"
    assert isinstance(test, SimpleTest)
    assert test.function_args == current.function_args
    assert "[Make('Graph', [('A', 'B')]), 'A']" in _texts(cli, "update_main_content")


# --- GeneralTests ---

GENERAL_SOURCE = """def test_add(solution):
    assert solution.add(1, 2) == 3
"""

GENERAL_SOLUTION = "def add(a, b):\n    return a + b\n"


def _run_general(
    keys: list[str], ex_files: list[Path], sol_files: list[Path]
) -> tuple[GeneralTest | None, MagicMock]:
    q: queue.Queue[str] = queue.Queue()
    for key in [*keys, "\x03"]:
        q.put(key)
    cli = MagicMock()
    test = PromptSession(cli, q).run(
        lambda s: _prompt_test(s, GeneralTest, ex_files, sol_files=sol_files)
    )
    assert q.get_nowait() == "\x03", "all keys were used"
    assert test is None or isinstance(test, GeneralTest)
    return test, cli


def _titles(cli: MagicMock) -> list[str]:
    return [c.kwargs["title"] for c in cli.update_main_content.call_args_list]


@pytest.fixture
def solution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    path = tmp_path / "sol.py"
    path.write_text(GENERAL_SOLUTION)
    return path


def test_general_test_from_a_file(
    tmp_path: Path, ex_files: list[Path], solution: Path
) -> None:
    source = tmp_path / "test_add.py"
    source.write_text(GENERAL_SOURCE)
    (tmp_path / "no_tests.py").write_text("x = 1\n")
    test, cli = _run_general(
        [
            DOWN, ENTER, *_type("general"), ENTER,  # no info file, name, file_path
            ENTER,  # source: from a file
            *_type(str(tmp_path / "no_tests.py")),  # defines no tests...
            *_clear("no_tests.py"), *_type("test_add.py"),  # ...fixed
            ENTER, ENTER, ENTER,  # traceable, isolated, soft: keep
            ENTER,  # checked on the only solution file: keep
        ],
        ex_files,
        [solution],
    )  # fmt: skip
    assert test is not None
    assert test.source == GENERAL_SOURCE
    assert f"Checked on {solution}: 1 passed, 0 failed" in _titles(cli)


def test_general_test_typed_in(ex_files: list[Path], solution: Path) -> None:
    test, _ = _run_general(
        [
            DOWN, ENTER, *_type("general"), ENTER,
            DOWN, ENTER,  # source: type it
            *"def test_add(solution):", ENTER,
            *"    assert solution.add(1, 2) == 3", CTRL_D,
            ENTER, ENTER, ENTER, ENTER,  # traceable, isolated, soft, keep
        ],
        ex_files,
        [solution],
    )  # fmt: skip
    assert test is not None
    assert test.source == GENERAL_SOURCE.rstrip("\n")


def test_general_test_is_checked_against_the_solution(
    tmp_path: Path, ex_files: list[Path], solution: Path
) -> None:
    failing = GENERAL_SOURCE + (
        "\ndef test_wrong(solution):\n    assert solution.add(1, 1) == 3\n"
    )
    (tmp_path / "failing.py").write_text(failing)
    (tmp_path / "fixed.py").write_text(GENERAL_SOURCE)
    test, cli = _run_general(
        [
            DOWN, ENTER, *_type("general"), ENTER,
            ENTER, *_type(str(tmp_path / "failing.py")),  # source: from a file
            ENTER, ENTER, ENTER,  # traceable, isolated, soft
            DOWN, ENTER,  # one test fails: edit again
            UP, ENTER, *_type(str(tmp_path / "fixed.py")),  # from another file
            ENTER,  # keep
        ],
        ex_files,
        [solution],
    )  # fmt: skip
    assert test is not None
    assert test.source == GENERAL_SOURCE
    titles = _titles(cli)
    assert f"Checked on {solution}: 1 passed, 1 failed" in titles
    assert f"Checked on {solution}: 1 passed, 0 failed" in titles
    # the failures are shown below the choice
    prompt = next(
        c.args[0]
        for c in cli.update_main_content.call_args_list
        if c.kwargs["title"].endswith("1 failed")
    )
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(prompt)
    shown = console.export_text()
    assert "::general::test_wrong failed" in shown
    assert "assert 2 == 3" in shown

    discarded, _ = _run_general(
        [
            DOWN, ENTER, *_type("general"), ENTER,
            ENTER, *_type(str(tmp_path / "failing.py")), ENTER, ENTER, ENTER,
            DOWN, DOWN, ENTER,  # discard
        ],
        ex_files,
        [solution],
    )  # fmt: skip
    assert discarded is None


def test_checks_are_cached_while_flows_are_replayed(
    tmp_path: Path,
    ex_files: list[Path],
    solution: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[object] = []
    real = create_task.run_tests

    def counted(*args: object, **kwargs: object):
        runs.append(args)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(create_task, "run_tests", counted)
    (tmp_path / "cached.py").write_text(GENERAL_SOURCE.replace("1, 2", "2, 1"))
    _run_general(
        [
            DOWN, ENTER, *_type("cached"), ENTER,
            ENTER, *_type(str(tmp_path / "cached.py")), ENTER, ENTER, ENTER,
            ESC, ENTER,  # back to soft, and on again: replayed
            ENTER,  # keep
        ],
        ex_files,
        [solution],
    )  # fmt: skip
    assert len(runs) == 1

    solution.write_text(GENERAL_SOLUTION + "\n")  # changed: checked again
    _run_general(
        [
            DOWN, ENTER, *_type("cached"), ENTER,
            ENTER, *_type(str(tmp_path / "cached.py")), ENTER, ENTER, ENTER, ENTER,
        ],
        ex_files,
        [solution],
    )  # fmt: skip
    assert len(runs) == 2


def test_the_solution_with_the_exercise_files_name_is_chosen(
    tmp_path: Path, ex_files: list[Path], solution: Path
) -> None:
    (tmp_path / "solutions").mkdir()
    same_name = tmp_path / "solutions" / ex_files[0].name
    same_name.write_text(GENERAL_SOLUTION)
    (tmp_path / "test_add.py").write_text(GENERAL_SOURCE)
    keys = [
        DOWN, ENTER, *_type("general"), ENTER,
        ENTER, *_type(str(tmp_path / "test_add.py")), ENTER, ENTER, ENTER,
    ]  # fmt: skip
    _, cli = _run_general([*keys, ENTER], ex_files, [solution, same_name])
    assert f"Checked on {same_name}: 1 passed, 0 failed" in _titles(cli)

    # otherwise, it is asked for, and the check can be skipped
    test, cli = _run_general(
        [*keys, DOWN, DOWN, ENTER], ex_files, [solution, tmp_path / "test_add.py"]
    )
    assert test is not None
    assert "Solution file to check the test against" in _titles(cli)
    assert not any(t.startswith("Checked on") for t in _titles(cli))
