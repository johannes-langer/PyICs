"""
Drive the interactive test-creation flow of ``create_task`` with scripted key presses.
"""

import queue
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.scripts.create_task import (
    _TEST_TYPES,
    _check_file,
    _prompt_files,
    _prompt_test,
    _prompt_tests,
)
from pyics.tests.tests import PrintTest, SimpleTest, SpecTest, StructureTest

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
            *_type("exp"),  # expected
            BACKSPACE, BACKSPACE, *_type("args[1:]"),  # function_args, prefilled with []
            ENTER,  # function_kwargs: keep {}
            ENTER,  # traceable: keep True
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


def test_missing_info_file_is_asked_again(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text(INFO_FILE)
    test = _run(
        [
            ENTER,  # reference a file: yes
            *_type(str(tmp_path / "info")),  # no such file...
            *_type(".py"),  # ...fixed, keeping the rejected input
            ENTER, *_type("exp"), ENTER, ENTER, ENTER,  # file_path, expected, defaults
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
            *_type("f"),  # function_name
            *_type("1 +"),  # expected: syntax error...
            *_type(" 1"),  # ...fixed, keeping the rejected input
            ENTER, ENTER, ENTER,  # function_args, function_kwargs, traceable: defaults
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
            ENTER,  # file_path
            ENTER, ENTER, ENTER,  # function_args, function_kwargs, traceable: defaults
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
            *_type("f"),  # function_name
            *_type("1"),  # expected
            ENTER, ENTER, ENTER,  # function_args, function_kwargs, traceable: defaults
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
            *_type("t"), ENTER, *_type("f"),  # name, file_path, function_name
            *_type("1"),  # expected
            ENTER, ENTER,  # function_args, function_kwargs: keep defaults
            *[BACKSPACE] * len("True"), *_type("1"),  # traceable: not a bool...
            BACKSPACE, *_type("False"),  # ...fixed
        ],
        ex_files,
    )  # fmt: skip
    assert test.traceable is False


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
            ENTER,  # by_type: keep False
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
            ENTER,  # by_type: keep False
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
            ENTER,  # by_type: keep False
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
            ENTER,  # traceable: keep True
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
            ENTER,  # traceable: keep True
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
            ENTER,  # traceable: keep True
        ],
        ex_files,
    )  # fmt: skip
    assert (test.expected, test.inputs) == ("", ["Bob"])


def test_print_test_is_offered(ex_files: list[Path]) -> None:
    tests = _run_flow(
        [
            *[DOWN] * _TEST_TYPES.index(PrintTest), ENTER,  # PrintTest
            DOWN, ENTER, *_type("t"), ENTER,  # no info file, name, file_path
            CTRL_D, ENTER, ENTER,  # expected: no output, inputs and traceable: keep
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
        ],
        lambda s: _prompt_test(s, StructureTest, ex_files),
    )  # fmt: skip
    assert isinstance(test, StructureTest)
    assert test.rules == [{"concept": "loop", "max": 0}]
    