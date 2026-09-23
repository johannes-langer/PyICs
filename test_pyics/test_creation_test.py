"""
Drive the interactive test-creation flow of ``create_task`` with scripted key presses.
"""

import queue
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.scripts.create_task import _prompt_test
from pyics.tests.tests import SimpleTest

DOWN, ENTER = "DOWN", "\r"

INFO_FILE = """
edges = [("A", "B", 4), ("B", "C", 5)]
all_edges = edges + [(b, a, c) for a, b, c in edges]

name = "Search path test"
function_name = "depth_first_search"

exp = ("A", "B", "C")
args = [all_edges, "A", "C"]
"""


def _run(keys: list[str], ex_files: list[Path]) -> SimpleTest:
    q: queue.Queue[str] = queue.Queue()
    # a trailing ctrl-c cancels instead of hanging if the flow asks for more than scripted
    for key in [*keys, "\x03"]:
        q.put(key)
    test = PromptSession(MagicMock(), q).run(
        lambda s: _prompt_test(s, SimpleTest, ex_files)
    )
    assert q.get_nowait() == "\x03", "flow finished before all scripted keys were used"
    assert isinstance(test, SimpleTest)
    return test


def _type(text: str) -> list[str]:
    return [*text, ENTER]


@pytest.fixture
def ex_files(tmp_path: Path) -> list[Path]:
    ex = tmp_path / "ex.py"
    ex.touch()
    return [ex]


def test_prefills_from_info_file(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text(INFO_FILE)
    test = _run(
        [
            ENTER,  # reference a file: yes
            *_type(str(info)),
            # name, function_name: exact matches
            ENTER,  # file_path: the only exercise file
            ENTER,  # expected: select a symbol...
            DOWN, DOWN, ENTER,  # ...exp (of edges, all_edges, exp, args)
            ENTER,  # function_args: select a symbol...
            DOWN, DOWN, ENTER,  # ...args (exp is taken)
            DOWN, DOWN, ENTER,  # function_kwargs: leave empty
        ],
        ex_files,
    )  # fmt: skip
    assert test.name == "Search path test"
    assert test.function_name == "depth_first_search"
    assert test.file_path == ex_files[0].absolute()
    assert test.expected == ("A", "B", "C")
    assert test.function_args[1:] == ["A", "C"]
    assert test.function_kwargs == {}


def test_file_path_prefilled_if_exercise_file(
    tmp_path: Path, ex_files: list[Path]
) -> None:
    info = tmp_path / "info.py"
    info.write_text(
        f"name = 'n'\nfunction_name = 'f'\nexpected = 1\nfile_path = {str(ex_files[0])!r}"
    )
    test = _run(
        [
            ENTER,
            *_type(str(info)),
            DOWN, ENTER,  # function_args: enter value
            ENTER,  # prefilled with []
            DOWN, ENTER,  # function_kwargs: leave empty
        ],
        ex_files,
    )  # fmt: skip
    assert test.file_path == ex_files[0].absolute()
    assert test.expected == 1
    assert test.function_args == []


def test_unusable_match_is_prompted_for(tmp_path: Path, ex_files: list[Path]) -> None:
    info = tmp_path / "info.py"
    info.write_text("name = 5\nfunction_name = 'f'\nexpected = 1\n")
    test = _run(
        [
            ENTER,
            *_type(str(info)),
            *_type("n"),  # name isn't a str, and there are no other symbols to pick
            ENTER,  # file_path
            DOWN, ENTER,  # function_args: leave empty
            DOWN, ENTER,  # function_kwargs: leave empty
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
            *_type("1"),  # expected: required, so entered directly
            ENTER, ENTER,  # function_args: enter value, prefilled with []
            DOWN, ENTER,  # function_kwargs: leave empty
        ],
        ex_files,
    )  # fmt: skip
    assert (test.name, test.function_name, test.expected) == ("t", "f", 1)
    assert test.function_args == []
    assert test.function_kwargs == {}


def test_too_few_keys_cancels(ex_files: list[Path]) -> None:
    with pytest.raises(PromptCancelled):
        _run([DOWN, ENTER], ex_files)
