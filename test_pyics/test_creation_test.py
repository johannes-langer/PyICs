"""
Drive the interactive test-creation flow of ``create_task`` with scripted key presses.
"""

import queue
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.scripts.create_task import _check_file, _prompt_files, _prompt_test
from pyics.tests.tests import SimpleTest, SpecTest

DOWN, ENTER, BACKSPACE = "DOWN", "\r", "\x7f"

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
        ],
        ex_files,
    )  # fmt: skip
    assert test.name == "Search path test"
    assert test.function_name == "depth_first_search"
    assert test.file_path == ex_files[0].absolute()
    assert test.expected == ("A", "B", "C")
    assert test.function_args == ["A", "C"]
    assert test.function_kwargs == {}


def test_missing_info_file_is_asked_again(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text(INFO_FILE)
    test = _run(
        [
            ENTER,  # reference a file: yes
            *_type(str(tmp_path / "info")),  # no such file...
            *_type(".py"),  # ...fixed, keeping the rejected input
            ENTER, *_type("exp"), ENTER, ENTER,  # file_path, expected, keep defaults
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
            ENTER, ENTER,  # function_args, function_kwargs: keep defaults
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
            *_type("n"),  # name isn't a str
            ENTER,  # file_path
            ENTER, ENTER,  # function_args, function_kwargs: keep defaults
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
            ENTER, ENTER,  # function_args, function_kwargs: keep defaults
        ],
        ex_files,
    )  # fmt: skip
    assert (test.name, test.function_name, test.expected) == ("t", "f", 1)
    assert test.function_args == []
    assert test.function_kwargs == {}


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
