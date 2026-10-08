"""
Drive the interactive course-creation flow of ``create_course`` with scripted key presses.
"""

import queue
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import pyics.util.json_handling as json
from pyics.app.prompts import PromptCancelled, PromptSession, ReorderInput
from pyics.scripts.create_course import _check_path, _course_flow
from pyics.scripts.create_task import _TEST_TYPES
from pyics.util.discovery import find_task_files

UP, DOWN, ENTER, ESC, SPACE, CTRL_C = "UP", "DOWN", "\r", "\x1b", " ", "\x03"


def _task_file(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name}))  # only the name is read when searching


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


def _run(keys: list[str], found: dict[Path, str]) -> dict[str, Any]:
    s = _session(keys)
    fields = s.run(lambda s: _course_flow(s, found))
    assert s.q.get_nowait() == CTRL_C, (
        "flow finished before all scripted keys were used"
    )
    return fields


def _type(text: str) -> list[str]:
    return [*text, ENTER]


# --- searching for task files ---


def test_find_respects_max_depth(tmp_path: Path) -> None:
    _task_file(tmp_path / "a.pyicstask.json", "A")
    _task_file(tmp_path / "d1" / "b.pyicstask.json", "B")
    _task_file(tmp_path / "d1" / "d2" / "c.pyicstask.json", "C")
    assert find_task_files(tmp_path, 1) == {
        Path("a.pyicstask.json"): "A",
        Path("d1/b.pyicstask.json"): "B",
    }
    assert len(find_task_files(tmp_path, 2)) == 3
    assert find_task_files(tmp_path, 0) == {Path("a.pyicstask.json"): "A"}


def test_find_skips_dirs_and_unreadable_files(tmp_path: Path) -> None:
    _task_file(tmp_path / ".tasks" / "a.pyicstask.json", "A")
    _task_file(tmp_path / ".venv" / "b.pyicstask.json", "B")
    _task_file(tmp_path / "c.json", "C")  # not a task file
    (tmp_path / "broken.pyicstask.json").write_text("{")
    (tmp_path / "nameless.pyicstask.json").write_text("{}")
    assert find_task_files(tmp_path, 3) == {Path(".tasks/a.pyicstask.json"): "A"}


# --- ReorderInput ---


def _press(prompt: ReorderInput, keys: list[str]) -> None:
    for key in keys:
        assert not prompt.handle_key(key)


def test_reorder_selecting_keeps_order() -> None:
    prompt = ReorderInput("order", ["a", "b", "c"])
    _press(prompt, [DOWN, DOWN, DOWN, UP])
    assert prompt.index == 1
    assert prompt.parse(prompt.answer()) == ["a", "b", "c"]


def test_reorder_moves_grabbed_option() -> None:
    prompt = ReorderInput("order", ["a", "b", "c"])
    _press(prompt, [UP, SPACE, UP, DOWN, DOWN, DOWN])
    prompt.render_body()  # renders the grabbed option
    _press(prompt, [SPACE, UP])
    assert prompt.index == 1
    assert prompt.parse(prompt.answer()) == ["b", "c", "a"]
    assert prompt.handle_key(ENTER)


def test_reorder_answer_round_trip() -> None:
    prompt = ReorderInput("order", ["a", "b", "c"])
    prompt.set_answer("2,0,1")
    assert prompt.answer() == "2,0,1"
    assert prompt.parse(prompt.answer()) == ["c", "a", "b"]


@pytest.mark.parametrize("answer", ["0,0,1", "0,1", "0,1,3", "a,b,c", ""])
def test_reorder_rejects_non_permutations(answer: str) -> None:
    prompt = ReorderInput("order", ["a", "b", "c"])
    with pytest.raises(ValueError):
        prompt.parse(answer)
    prompt.set_answer(answer)  # ignored
    assert prompt.answer() == "0,1,2"


# --- the course flow ---

FOUND = {Path("a.pyicstask.json"): "A", Path("sub/b.pyicstask.json"): "B"}


def test_existing_tasks_reordered() -> None:
    fields = _run(
        [
            *_type("Course"),
            ENTER, ENTER,  # add an existing task: A
            ENTER, ENTER,  # add an existing task: B, the only one left
            DOWN, ENTER,  # existing ones are all added, so: create new, done
            SPACE, DOWN, SPACE, ENTER,  # move A below B
        ],
        FOUND,
    )  # fmt: skip
    assert fields == {
        "name": "Course",
        "tasks": [
            (Path("sub/b.pyicstask.json"), None),
            (Path("a.pyicstask.json"), None),
        ],
    }


def test_back_from_reorder_returns_to_task_loop() -> None:
    fields = _run(
        [
            *_type("Course"),
            ENTER, ENTER, ENTER, ENTER, DOWN, ENTER,  # add A and B, done
            ESC,  # back from reordering...
            ENTER,  # ...to the task menu, prefilled with done
            SPACE, DOWN, SPACE, ENTER,
        ],
        FOUND,
    )  # fmt: skip
    assert [path for path, _ in fields["tasks"]] == [
        Path("sub/b.pyicstask.json"),
        Path("a.pyicstask.json"),
    ]


def test_done_not_offered_without_tasks() -> None:
    s = _session([*_type("Course"), DOWN, ENTER])  # only "create a new task" is offered
    with pytest.raises(PromptCancelled):
        s.run(lambda s: _course_flow(s, {}))
    assert _cli(s).update_main_content.call_args.kwargs["title"] == "Task name"


def test_duplicate_task_name_is_rejected() -> None:
    found = {Path("a.pyicstask.json"): "A", Path("b.pyicstask.json"): "A"}
    s = _session([*_type("Course"), ENTER, ENTER, ENTER, ENTER])
    with pytest.raises(PromptCancelled):
        s.run(lambda s: _course_flow(s, found))
    info = [c.args[0] for c in _cli(s).update_info.call_args_list]
    assert any("the course already has a task named 'A'" in i for i in info)


def test_new_task_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ex, sol = tmp_path / "ex.py", tmp_path / "sol.py"
    ex.touch()
    sol.touch()
    fields = _run(
        [
            *_type("Course"),
            ENTER,  # create a new task, the only option
            *_type("Task"),
            "I", "\x04",  # instructions
            *_type(str(ex)), DOWN, ENTER,  # one exercise file
            ENTER, *_type(str(sol)), DOWN, ENTER,  # add a solution file: yes, just one
            *[DOWN] * len(_TEST_TYPES), ENTER,  # no tests
            ENTER,  # task file path: keep the default
            DOWN, ENTER,  # done, no reordering for a single task
        ],
        {},
    )  # fmt: skip
    assert fields["tasks"] == [
        (
            Path("./Task.pyicstask.json"),
            {
                "name": "Task",
                "instructions": "I",
                "ex_files": [ex],
                "sol_files": [sol],
                "tests": None,
                "file_contents": {},
            },
        )
    ]
    assert list(tmp_path.glob("*.pyicstask.json")) == []


def test_check_path(tmp_path: Path) -> None:
    tasks = {"A": (Path("a.pyicstask.json"), None)}
    assert _check_path("b.pyicstask.json", tasks) == Path("b.pyicstask.json")
    for raw in ["", str(tmp_path), "./a.pyicstask.json"]:
        with pytest.raises(ValueError):
            _check_path(raw, tasks)
