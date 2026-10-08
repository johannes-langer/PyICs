"""
Edit tasks: read a task file back into the keyword arguments it was created from, and
drive the task-creation flow of ``create_task`` prefilled with them (as ``edit_task``
does) with scripted key presses.
"""

import io
import queue
from collections.abc import Callable, Collection
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

import pyics.util.json_handling as json
from pyics.app.prompts import PromptSession
from pyics.data import Course, Task
from pyics.scripts import edit_task
from pyics.scripts.create_task import _prompt_files, _task_flow
from pyics.tests.tests import SimpleTest

UP, DOWN, ENTER, BACKSPACE, CTRL_C, CTRL_D = "UP", "DOWN", "\r", "\x7f", "\x03", "\x04"

TEMPLATE = "def f():\n    return 0\n"
SOLUTION = "def f():\n    return 1\n"


def _type(text: str) -> list[str]:
    return [*text, ENTER]


def _clear(text: str) -> list[str]:
    return [BACKSPACE] * len(text)


def _session(keys: list[str]) -> PromptSession:
    q: queue.Queue[str] = queue.Queue()
    # a trailing ctrl-c cancels instead of hanging if the flow asks for more than scripted
    for key in [*keys, CTRL_C]:
        q.put(key)
    return PromptSession(MagicMock(), q)


def _cli(s: PromptSession) -> MagicMock:
    """The mock standing in for the CLI of the session."""
    assert isinstance(s.cli, MagicMock)
    return s.cli


def _run[T](s: PromptSession, flow: Callable[[PromptSession], T]) -> T:
    result = s.run(flow)
    assert s.q.get_nowait() == CTRL_C, (
        "flow finished before all scripted keys were used"
    )
    return result


def _edit(
    keys: list[str], task_file: Path, taken_names: Collection[str] = ()
) -> tuple[dict[str, Any], PromptSession]:
    current = Task.read_task_json(task_file)
    s = _session(keys)
    fields = _run(s, lambda s: _task_flow(s, taken_names=taken_names, current=current))
    return fields, s


def _shown(s: PromptSession, title: str) -> str:
    """The text of the last prompt with the label ``title``, as the CLI shows it."""
    calls = _cli(s).update_main_content.call_args_list
    renderable = next(c.args[0] for c in reversed(calls) if c.kwargs["title"] == title)
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def _info(s: PromptSession) -> list[str]:
    return [c.args[0] for c in _cli(s).update_info.call_args_list]


@pytest.fixture
def task_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A task with an exercise file, a solution file and a test, in the working
    directory."""
    monkeypatch.chdir(tmp_path)
    Path("ex.py").write_text(TEMPLATE)
    Path("sol.py").write_text(SOLUTION)
    test = SimpleTest("returns 1", Path("ex.py"), "f", 1)
    return Task.create_task_json(
        Path("t"), "T", "Do it", Path("ex.py"), Path("sol.py"), test
    )


# confirms every prompt of the edit flow for the task of the ``task_file`` fixture
KEEP_ALL = [
    ENTER,  # name
    CTRL_D,  # instructions
    ENTER,  # add a hint: no
    ENTER, ENTER,  # keep ex.py, add another exercise file: no
    ENTER, ENTER,  # keep sol.py, add another solution file: no
    ENTER,  # keep the test
    ENTER,  # done adding tests
]  # fmt: skip


# --- reading task files ---


def test_read_task_json_round_trip(task_file: Path) -> None:
    original = task_file.read_text()
    fields = Task.read_task_json(task_file)
    assert fields["ex_files"] == [Path("ex.py")]
    assert fields["file_contents"] == {
        Path("ex.py"): TEMPLATE,
        Path("sol.py"): SOLUTION,
    }
    assert fields["tests"][0].file_path == Path("ex.py")

    Task.create_task_json(task_file, **fields)
    assert task_file.read_text() == original


def test_stored_contents_are_used_instead_of_the_files(task_file: Path) -> None:
    Path("ex.py").unlink()  # e.g. a fresh clone of a course, without exercise files
    Path("sol.py").write_text("changed")

    Task.create_task_json(task_file, **Task.read_task_json(task_file))
    raw = json.loads(task_file.read_text())
    assert raw["exercise_code"] == [TEMPLATE]
    assert raw["solution_code"] == [SOLUTION]


def test_read_task_json_resolves_paths_relative_to_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("ex").mkdir()
    Path("ex/a.py").write_text(TEMPLATE)
    task_file = Task.create_task_json(
        Path("tasks/a"),
        "A",
        "",
        Path("ex/a.py"),
        None,
        SimpleTest("t", "ex/a.py", "f", 0),
    )

    monkeypatch.chdir(tmp_path / "ex")
    fields = Task.read_task_json(tmp_path / task_file)
    assert fields["ex_files"] == [tmp_path / "ex/a.py"]
    assert fields["tests"][0].file_path == tmp_path / "ex/a.py"


# --- the edit flow ---


def test_confirming_everything_keeps_the_task(task_file: Path) -> None:
    original = task_file.read_text()
    fields, _ = _edit(KEEP_ALL, task_file)
    assert fields["name"] == "T"
    assert fields["instructions"] == "Do it"

    Task.create_task_json(task_file, **fields)
    assert task_file.read_text() == original


def test_rename_and_change_instructions(task_file: Path) -> None:
    fields, _ = _edit(
        [
            *_clear("T"), *_type("Renamed"),
            *_clear("it"), *"one", CTRL_D,  # "Do it" -> "Do one"
            *KEEP_ALL[2:],
        ],
        task_file,
    )  # fmt: skip
    assert (fields["name"], fields["instructions"]) == ("Renamed", "Do one")


def test_taken_name_is_rejected(task_file: Path) -> None:
    fields, s = _edit(
        [*_clear("T"), *_type("Other"), *_clear("Other"), *_type("New"), *KEEP_ALL[1:]],
        task_file,
        taken_names={"Other"},
    )
    assert fields["name"] == "New"
    assert any("already has a task named 'Other'" in i for i in _info(s))


def test_missing_file_keeps_stored_contents(task_file: Path) -> None:
    Path("ex.py").unlink()
    fields, s = _edit(KEEP_ALL, task_file)
    assert "isn't on disk" in _shown(s, "Exercise file ex.py")
    assert fields["file_contents"][Path("ex.py")] == TEMPLATE

    Task.create_task_json(task_file, **fields)
    assert json.loads(task_file.read_text())["exercise_code"] == [TEMPLATE]


@pytest.mark.parametrize("update", [False, True])
def test_changed_file_is_shown_and_kept_or_updated(
    task_file: Path, update: bool
) -> None:
    Path("ex.py").write_text(TEMPLATE.replace("0", "2"))
    choose = [DOWN, ENTER] if update else [ENTER]  # update / keep
    fields, s = _edit([*KEEP_ALL[:3], *choose, *KEEP_ALL[4:]], task_file)

    shown = _shown(s, "Exercise file ex.py")
    assert "Update" in shown
    assert "-    return 0" in shown and "+    return 2" in shown

    Task.create_task_json(task_file, **fields)
    expected = TEMPLATE.replace("0", "2") if update else TEMPLATE
    assert json.loads(task_file.read_text())["exercise_code"] == [expected]


def test_update_is_only_offered_for_changed_files(task_file: Path) -> None:
    _, s = _edit(KEEP_ALL, task_file)
    assert "Update" not in _shown(s, "Exercise file ex.py")


def test_replaced_file_keeps_its_tests(task_file: Path) -> None:
    Path("new.py").write_text(TEMPLATE)
    fields, _ = _edit(
        [
            *KEEP_ALL[:3],
            DOWN, ENTER, *_clear("ex.py"), *_type("new.py"),  # replace ex.py
            *KEEP_ALL[4:],  # the test can be kept
        ],
        task_file,
    )  # fmt: skip
    assert fields["ex_files"] == [Path("new.py")]
    assert [t.file_path for t in fields["tests"]] == [Path("new.py")]
    assert Path("ex.py") not in fields["file_contents"]

    # the task's own test is unchanged, since the flow may be replayed
    assert Task.read_task_json(task_file)["tests"][0].file_path == Path("ex.py")


def test_tests_of_a_removed_file_cant_be_kept(task_file: Path) -> None:
    Path("new.py").write_text(TEMPLATE)
    fields, s = _edit(
        [
            *KEEP_ALL[:3],
            DOWN, DOWN, ENTER,  # remove ex.py
            *_type("new.py"),  # a task needs an exercise file
            ENTER,  # add another exercise file: no
            *KEEP_ALL[5:7],
            DOWN, ENTER,  # the test can only be edited or removed: remove it
            ENTER,
        ],
        task_file,
    )  # fmt: skip
    assert fields["ex_files"] == [Path("new.py")]
    assert fields["tests"] is None
    shown = _shown(s, "Test 1 of 1: returns 1")
    assert "isn't one of the task's exercise files" in shown
    assert "Keep" not in shown


def test_edit_test_prefilled(task_file: Path) -> None:
    fields, _ = _edit(
        [
            *KEEP_ALL[:7],
            DOWN, ENTER,  # edit the test
            ENTER,  # reference a file with test information: no
            ENTER, ENTER, ENTER,  # name, file_path, function_name: keep
            *_clear("1"), *_type("2"),  # expected
            ENTER, ENTER, ENTER,  # function_args, function_kwargs, traceable: keep
            ENTER,
        ],
        task_file,
    )  # fmt: skip
    (test,) = fields["tests"]
    assert isinstance(test, SimpleTest)
    assert (test.name, test.function_name, test.expected) == ("returns 1", "f", 2)
    assert test.file_path == Path("ex.py").absolute()


def test_edit_test_of_a_removed_file(task_file: Path) -> None:
    Path("new.py").write_text(TEMPLATE)
    fields, _ = _edit(
        [
            *KEEP_ALL[:3],
            DOWN, DOWN, ENTER, *_type("new.py"), ENTER,  # remove ex.py, add new.py
            *KEEP_ALL[5:7],
            ENTER,  # edit the test, preselected since it can't be kept
            ENTER, ENTER,  # no test information file, name: keep
            ENTER,  # file_path: new.py, the only exercise file
            ENTER, ENTER, ENTER, ENTER, ENTER,  # keep the other parameters
            ENTER,
        ],
        task_file,
    )  # fmt: skip
    assert [t.file_path for t in fields["tests"]] == [Path("new.py").absolute()]


def test_values_without_a_python_repr_are_prefilled_by_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("ex.py").write_text(TEMPLATE)
    test = SimpleTest("t", Path("ex.py"), "f", 1, function_args=[Path("a")])
    task_file = Task.create_task_json(Path("t"), "T", "", Path("ex.py"), None, test)

    fields, s = _edit(
        [
            ENTER, CTRL_D, ENTER, ENTER, ENTER, ENTER,  # name, instructions, hints, files
            DOWN, ENTER,  # edit the test
            ENTER,  # reference a file with test information: no
            ENTER, ENTER, ENTER, ENTER,  # name, file_path, function_name, expected
            ENTER,  # function_args, prefilled with a reference to the current value
            ENTER,  # function_kwargs
            ENTER,  # traceable
            ENTER,
        ],
        task_file,
    )  # fmt: skip
    assert fields["tests"][0].function_args == [Path("a")]
    assert f"current = [{Path('a')!r}]" in _shown(s, "function_args")


def test_strings_are_prefilled_despite_test_information(task_file: Path) -> None:
    Path("info.py").write_text("other = 'x'\n")
    fields, _ = _edit(
        [
            *KEEP_ALL[:7],
            DOWN, ENTER,  # edit the test
            UP, ENTER, *_type("info.py"),  # reference a file with test information
            ENTER, ENTER,  # name: enter text (preselected), keep it
            ENTER,  # file_path
            ENTER, ENTER,  # function_name: enter text (preselected), keep it
            ENTER, ENTER, ENTER,  # expected, function_args, function_kwargs
            ENTER,  # traceable
            ENTER,
        ],
        task_file,
    )  # fmt: skip
    assert (fields["tests"][0].name, fields["tests"][0].function_name) == (
        "returns 1",
        "f",
    )


def test_adding_a_file_twice_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("a.py").touch()
    Path("b.py").touch()
    s = _session(
        [
            *_type("a.py"),
            ENTER,  # add another exercise file: yes
            *_type("./a.py"),  # already added...
            *_clear("./a.py"), *_type("b.py"),  # ...fixed
            DOWN, ENTER,
        ]
    )  # fmt: skip
    assert _run(s, lambda s: _prompt_files(s, "exercise")) == [
        Path("a.py"),
        Path("b.py"),
    ]
    assert any("already one of the task's files" in i for i in _info(s))


# --- hints ---


@pytest.fixture
def hinted_task_file(task_file: Path) -> Path:
    """The task of the ``task_file`` fixture, with two hints."""
    fields = Task.read_task_json(task_file)
    return Task.create_task_json(task_file, **{**fields, "help": ["first", "second"]})


def test_hints_round_trip(hinted_task_file: Path) -> None:
    original = hinted_task_file.read_text()
    fields = Task.read_task_json(hinted_task_file)
    assert fields["help"] == ["first", "second"]
    assert Task(hinted_task_file, setup=False).help == ["first", "second"]

    Task.create_task_json(hinted_task_file, **fields)
    assert hinted_task_file.read_text() == original


def test_a_task_file_without_hints_has_none(task_file: Path) -> None:
    raw = json.loads(task_file.read_text())
    del raw["help"]  # e.g. a task file written before tasks had hints
    task_file.write_text(json.dumps(raw))
    assert Task.read_task_json(task_file)["help"] == []
    assert Task(task_file, setup=False).help == []


def test_a_single_hint_can_be_given_as_a_string(task_file: Path) -> None:
    Task.create_task_json(task_file, **{**Task.read_task_json(task_file), "help": "h"})
    assert Task.read_task_json(task_file)["help"] == ["h"]


def test_confirming_everything_keeps_the_hints(hinted_task_file: Path) -> None:
    original = hinted_task_file.read_text()
    fields, _ = _edit(
        [*KEEP_ALL[:2], ENTER, ENTER, *KEEP_ALL[2:]],  # keep both hints, add none
        hinted_task_file,
    )
    assert fields["help"] == ["first", "second"]

    Task.create_task_json(hinted_task_file, **fields)
    assert hinted_task_file.read_text() == original


def test_add_hints(task_file: Path) -> None:
    fields, _ = _edit(
        [
            *KEEP_ALL[:2],
            UP, ENTER, *"Look at f.", CTRL_D,  # add a hint: yes
            UP, ENTER, *"Return 1.", CTRL_D,  # add another hint: yes
            ENTER,  # add another hint: no
            *KEEP_ALL[3:],
        ],
        task_file,
    )  # fmt: skip
    assert fields["help"] == ["Look at f.", "Return 1."]


def test_edit_and_remove_hints(hinted_task_file: Path) -> None:
    fields, s = _edit(
        [
            *KEEP_ALL[:2],
            DOWN, ENTER, *_clear("first"), *"1st", CTRL_D,  # edit the first hint
            DOWN, DOWN, ENTER,  # remove the second hint
            ENTER,  # add another hint: no
            *KEEP_ALL[3:],
        ],
        hinted_task_file,
    )  # fmt: skip
    assert fields["help"] == ["1st"]
    assert "second" in _shown(s, "Hint 2 of 2")


def test_empty_hints_are_rejected(task_file: Path) -> None:
    fields, s = _edit(
        [
            *KEEP_ALL[:2],
            UP, ENTER, *"  ", CTRL_D,  # add a hint: yes, but it's empty...
            *_clear("  "), *"Return 1.", CTRL_D,  # ...fixed
            ENTER,  # add another hint: no
            *KEEP_ALL[3:],
        ],
        task_file,
    )  # fmt: skip
    assert fields["help"] == ["Return 1."]
    assert any("the hint may not be empty" in i for i in _info(s))


# --- the edit_task script ---


def _run_script(
    monkeypatch: pytest.MonkeyPatch, keys: list[str], task_file: Path
) -> None:
    q: queue.Queue[str] = queue.Queue()
    for key in [*keys, CTRL_C]:
        q.put(key)
    monkeypatch.setattr(edit_task, "CLI", MagicMock)
    monkeypatch.setattr(edit_task, "KeyListener", lambda: nullcontext(q))
    edit_task._edit_task(task_file)
    assert q.get_nowait() == CTRL_C, (
        "script finished before all scripted keys were used"
    )


def _course(fp: Path, tasks: dict[str, Path]) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps({"name": fp.stem, "tasks": tasks, "progress": [0, 1]}))


def test_rename_carries_over_to_courses(
    task_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    other = Task.create_task_json(Path("o"), "O", "", Path("ex.py"))
    _course(Path("c.pyicscourse.json"), {"T": task_file, "O": other})
    # stored relative to the manifest, in a subdirectory
    _course(Path("sub/d.pyicscourse.json"), {"O": Path("../o.pyicstask.json")})
    _course(
        Path("sub/e.pyicscourse.json"),
        {"X": Path("../o.pyicstask.json"), "T": Path("../t.pyicstask.json")},
    )
    untouched = Path("sub/d.pyicscourse.json").read_text()

    _run_script(monkeypatch, [*_clear("T"), *_type("New"), *KEEP_ALL[1:]], task_file)

    assert Task.read_task_json(task_file)["name"] == "New"
    course = json.loads(Path("c.pyicscourse.json").read_text())
    assert course["tasks"] == {"New": Path("t.pyicstask.json"), "O": other}
    assert course["progress"] == [0, 1]
    e = json.loads(Path("sub/e.pyicscourse.json").read_text())
    assert list(e["tasks"]) == ["X", "New"]  # same position
    assert Path("sub/d.pyicscourse.json").read_text() == untouched

    message = capsys.readouterr().out
    assert "c.pyicscourse.json" in message and "e.pyicscourse.json" in message
    assert "d.pyicscourse.json" not in message

    # the course still loads, with the renamed task
    monkeypatch.setattr(Task, "run_tests", lambda self: None)
    assert Course.load_course_manifest(Path("c.pyicscourse.json")).tasks == {
        "New": Path("t.pyicstask.json"),
        "O": Path("o.pyicstask.json"),
    }


def test_name_of_another_task_in_a_course_is_rejected(
    task_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Task.create_task_json(Path("o"), "O", "", Path("ex.py"))
    _course(Path("c.pyicscourse.json"), {"T": task_file, "O": other})
    _run_script(
        monkeypatch,
        [*_clear("T"), *_type("O"), *_clear("O"), *_type("P"), *KEEP_ALL[1:]],
        task_file,
    )
    assert list(json.loads(Path("c.pyicscourse.json").read_text())["tasks"]) == [
        "P",
        "O",
    ]


def test_cancelling_leaves_the_task(
    task_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    original = task_file.read_text()
    _run_script(monkeypatch, [*_clear("T"), CTRL_C], task_file)
    assert task_file.read_text() == original
    assert "Cancelled" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["t.json", "missing.pyicstask.json"])
def test_script_rejects_other_files(task_file: Path, name: str) -> None:
    Path("t.json").write_text(task_file.read_text())
    with pytest.raises(SystemExit, match="error"):
        edit_task._edit_task(Path(name))
