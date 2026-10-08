"""
Edit courses: drive the course-creation flow of ``create_course`` prefilled with a course
(as ``edit_course`` does) with scripted key presses, and keep the other course manifests
consistent with the tasks that are renamed.
"""

import io
import queue
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

import pyics.util.json_handling as json
from pyics.app.prompts import PromptSession
from pyics.data import Course, Task
from pyics.scripts import edit_course
from pyics.scripts.create_course import (
    _course_flow,
    _names_taken_elsewhere,
    _rename_in_courses,
)
from pyics.scripts.create_task import _TEST_TYPES
from pyics.scripts.edit_course import _read_course
from pyics.util.discovery import find_courses

UP, DOWN, ENTER, BACKSPACE, SPACE = "UP", "DOWN", "\r", "\x7f", " "
CTRL_C, CTRL_D = "\x03", "\x04"

A, B, C = (Path(f".tasks/{n}.pyicstask.json") for n in "abc")

# confirms every prompt of the edit flow for the course of the ``course_file`` fixture
KEEP_ALL = [
    ENTER,  # name
    ENTER, ENTER, ENTER,  # keep A, B and C
    ENTER,  # done adding tasks
    ENTER,  # keep the order
]  # fmt: skip

# confirms every prompt of the edit flow for one of the fixture's tasks, after its name
KEEP_TASK = [
    CTRL_D,  # instructions
    ENTER, ENTER,  # keep its exercise file, add another one: no
    ENTER,  # add a solution file: no
    ENTER,  # done adding tests
]  # fmt: skip


def _type(text: str) -> list[str]:
    return [*text, ENTER]


def _clear(text: str) -> list[str]:
    return [BACKSPACE] * len(text)


def _task(name: str, path: Path | None = None) -> Path:
    """Write a task without tests, with an exercise file next to it."""
    path = path or Path(f".tasks/{name.lower()}")
    ex = path.parent / f"{name.lower()}.py"
    ex.parent.mkdir(parents=True, exist_ok=True)
    ex.write_text(f"# {name}\n")
    return Task.create_task_json(path, name, f"Do {name}", ex)


def _manifest(fp: Path) -> dict[str, Any]:
    return json.loads(fp.read_text())


@pytest.fixture
def course_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A course of the tasks A, B and C, of which A and C are solved, in the working
    directory."""
    monkeypatch.chdir(tmp_path)
    task_files = [_task(name) for name in "ABC"]
    return Course.create_course_manifest(
        Path("c"), task_files, name="Course", progress=[1, 0, 1]
    )


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


def _edit(
    keys: list[str],
    course_file: Path,
    found: dict[Path, str] | None = None,
    courses: dict[Path, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], PromptSession]:
    current, _ = _read_course(course_file, _manifest(course_file), courses or {})
    s = _session(keys)
    fields = s.run(lambda s: _course_flow(s, found or {}, current))
    assert s.q.get_nowait() == CTRL_C, (
        "flow finished before all scripted keys were used"
    )
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


def _run_script(
    monkeypatch: pytest.MonkeyPatch, keys: list[str], course_file: Path
) -> None:
    q: queue.Queue[str] = queue.Queue()
    for key in [*keys, CTRL_C]:
        q.put(key)
    monkeypatch.setattr(edit_course, "CLI", MagicMock)
    monkeypatch.setattr(edit_course, "KeyListener", lambda: nullcontext(q))
    edit_course._edit_course(course_file)
    assert q.get_nowait() == CTRL_C, (
        "script finished before all scripted keys were used"
    )


# --- manifests ---


def test_manifest_progress(tmp_path: Path) -> None:
    fp = Course.create_course_manifest(tmp_path / "c", [], progress=[])
    assert _manifest(fp)["progress"] == []
    with pytest.raises(ValueError):
        Course.create_course_manifest(tmp_path / "c", [], progress=[0])


def test_find_courses_skips_unusable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("sub").mkdir()
    Path("sub/c.pyicscourse.json").write_text(json.dumps({"tasks": {"A": Path("a")}}))
    Path("broken.pyicscourse.json").write_text("{")
    Path("tasksless.pyicscourse.json").write_text(json.dumps({"name": "x"}))
    Path("odd.pyicscourse.json").write_text(json.dumps({"tasks": {"A": 1}}))
    Path(".venv").mkdir()
    Path(".venv/v.pyicscourse.json").write_text(json.dumps({"tasks": {}}))
    assert list(find_courses(tmp_path, 3)) == [tmp_path / "sub/c.pyicscourse.json"]


def test_rename_in_courses(tmp_path: Path) -> None:
    fp = tmp_path / "sub/c.pyicscourse.json"
    fp.parent.mkdir()
    manifest = {
        "name": "c",
        "tasks": {"X": Path("../x.pyicstask.json"), "A": Path("../a.pyicstask.json")},
        "progress": [1, 0],
    }
    fp.write_text(json.dumps(manifest))
    courses = find_courses(tmp_path, 3)

    task_file = tmp_path / "a.pyicstask.json"
    assert _names_taken_elsewhere(task_file, courses) == {"X"}
    assert _names_taken_elsewhere(tmp_path / "b.pyicstask.json", courses) == set()

    assert _rename_in_courses(task_file, "A", courses) == []  # nothing to rename
    assert _rename_in_courses(task_file, "B", courses) == [fp]
    tasks = manifest["tasks"]
    expected = {**manifest, "tasks": {"X": tasks["X"], "B": tasks["A"]}}
    assert _manifest(fp) == expected
    assert courses[fp] == expected
    assert list(_manifest(fp)["tasks"]) == ["X", "B"]


# --- the edit flow ---


def test_confirming_everything_keeps_the_course(course_file: Path) -> None:
    fields, _ = _edit(KEEP_ALL, course_file)
    assert fields == {"name": "Course", "tasks": [(A, None), (B, None), (C, None)]}


def test_rename_to_a_name_in_the_course_is_rejected(course_file: Path) -> None:
    fields, s = _edit(
        [
            ENTER,
            DOWN, ENTER,  # edit A
            *_clear("A"), *_type("C"),  # the name of a task still to come...
            *_clear("C"), *_type("D"),  # ...fixed
            *KEEP_TASK, *KEEP_ALL[2:],
        ],
        course_file,
    )  # fmt: skip
    assert [f and f["name"] for _, f in fields["tasks"]] == ["D", None, None]
    assert any("already has a task named 'C'" in i for i in _info(s))


def test_rename_to_a_name_in_another_course_is_rejected(course_file: Path) -> None:
    x = _task("X")
    Course.create_course_manifest(Path("o.pyicscourse.json"), [x, A])
    courses = find_courses(Path.cwd(), 3)
    del courses[course_file.absolute()]  # like edit_course
    fields, s = _edit(
        [
            ENTER,
            DOWN, ENTER, *_clear("A"), *_type("X"), *_clear("X"), *_type("D"),
            *KEEP_TASK, *KEEP_ALL[2:],
        ],
        course_file,
        courses=courses,
    )  # fmt: skip
    assert fields["tasks"][0][1]["name"] == "D"
    assert any("already has a task named 'X'" in i for i in _info(s))


def test_unreadable_task_can_only_be_removed(course_file: Path) -> None:
    B.unlink()
    fields, s = _edit(KEEP_ALL, course_file)  # enter removes B, the only option
    assert fields["tasks"] == [(A, None), (C, None)]
    shown = _shown(s, "Task 2 of 3: B")  # named as in the manifest
    assert "can't be read" in shown
    assert "Keep" not in shown


def test_task_named_like_another_one_must_be_renamed(course_file: Path) -> None:
    fields = Task.read_task_json(C)
    Task.create_task_json(C, **{**fields, "name": "A"})  # e.g. edited by hand
    fields, s = _edit(
        [
            ENTER, ENTER, ENTER,  # name, keep A and B
            ENTER,  # edit the second A, preselected since it can't be kept
            ENTER,  # its name is taken...
            *_clear("A"), *_type("C"),  # ...fixed
            *KEEP_TASK,
            *KEEP_ALL[4:],
        ],
        course_file,
    )  # fmt: skip
    assert [f and f["name"] for _, f in fields["tasks"]] == [None, None, "C"]
    assert "Another task of the course is named 'A'" in _shown(s, "Task 3 of 3: A")


def test_found_tasks_of_the_course_are_not_offered(course_file: Path) -> None:
    d = _task("D")
    found = {A: "A", B: "B", C: "C", d: "D"}
    fields, s = _edit(
        [
            *KEEP_ALL[:4],
            UP, UP, ENTER,  # add an existing task
            ENTER,  # D, the only one not in the course yet
            ENTER,  # done adding tasks, preselected
            ENTER,
        ],
        course_file.absolute(),  # task paths are absolute, found ones relative
        found,
    )  # fmt: skip
    assert fields["tasks"][-1] == (d, None)
    assert _shown(s, "Task file").count("pyicstask.json") == 1


# --- the edit_course script ---


def test_script_keeps_the_course(
    course_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = course_file.read_text()
    tasks = {p: p.read_text() for p in (A, B, C)}
    _run_script(monkeypatch, KEEP_ALL, course_file)
    assert course_file.read_text() == original
    assert {p: p.read_text() for p in (A, B, C)} == tasks


def test_progress_follows_the_tasks(
    course_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_script(
        monkeypatch,
        [
            ENTER,
            DOWN, DOWN, ENTER,  # remove A
            ENTER, ENTER, ENTER,  # keep B and C, done
            SPACE, DOWN, SPACE, ENTER,  # move B below C
        ],
        course_file,
    )  # fmt: skip
    manifest = _manifest(course_file)
    assert list(manifest["tasks"]) == ["C", "B"]
    assert manifest["progress"] == [1, 0]
    assert A.exists()  # removed from the course only


def test_edited_task_is_renamed_in_all_courses(
    course_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    other = Course.create_course_manifest(Path("sub/o"), [B, A], progress=[1, 0])
    unrelated = Course.create_course_manifest(Path("sub/u"), [B])
    unrelated_text = unrelated.read_text()

    _run_script(
        monkeypatch,
        [
            ENTER,
            DOWN, ENTER, *_clear("A"), *_type("A2"), *KEEP_TASK,  # rename A
            *KEEP_ALL[2:],
        ],
        course_file,
    )  # fmt: skip
    assert Task.read_task_json(A)["name"] == "A2"
    manifest = _manifest(course_file)
    assert list(manifest["tasks"]) == ["A2", "B", "C"]
    assert manifest["progress"] == [1, 0, 1]

    other_manifest = _manifest(other)
    assert other_manifest["tasks"] == {
        "B": Path("../.tasks/b.pyicstask.json"),
        "A2": Path("../.tasks/a.pyicstask.json"),
    }
    assert other_manifest["progress"] == [1, 0]
    assert unrelated.read_text() == unrelated_text
    assert "o.pyicscourse.json" in capsys.readouterr().out


def test_new_task_is_written(
    course_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("d.py").write_text("# D\n")
    _run_script(
        monkeypatch,
        [
            *KEEP_ALL[:4],
            UP, ENTER,  # create a new task
            *_type("D"), CTRL_D,  # name, instructions
            *_type("d.py"), DOWN, ENTER,  # one exercise file
            DOWN, ENTER,  # no solution file
            *[DOWN] * len(_TEST_TYPES), ENTER,  # no tests
            ENTER,  # task file path: keep the default
            ENTER, ENTER,  # done, keep the order
        ],
        course_file,
    )  # fmt: skip
    manifest = _manifest(course_file)
    assert list(manifest["tasks"]) == ["A", "B", "C", "D"]
    assert manifest["progress"] == [1, 0, 1, 0]
    assert Task.read_task_json(Path("D.pyicstask.json"))["name"] == "D"


@pytest.mark.parametrize("name", ["c.json", "missing.pyicscourse.json"])
def test_script_rejects_other_files(course_file: Path, name: str) -> None:
    Path("c.json").write_text(course_file.read_text())
    with pytest.raises(SystemExit, match="error"):
        edit_course._edit_course(Path(name))
