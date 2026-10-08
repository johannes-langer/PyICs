from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any, NamedTuple

from rich.console import Group
from rich.markup import escape
from rich.text import Text

import pyics.util.json_handling as json
from pyics.app.cli import CLI
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import (
    PathInput,
    PromptCancelled,
    PromptSession,
    ReorderInput,
    SelectInput,
    TextInput,
)
from pyics.data.course import Course
from pyics.data.task import Task
from pyics.scripts.create_task import _EDIT, _KEEP, _REMOVE, _check_name, _task_flow
from pyics.util import paths
from pyics.util.discovery import TASK_SUFFIX, find_task_files
from pyics.util.terminal import fullscreen

_TOTAL_STEPS = 3
_INFO = "Creating a new course..."

_ADD_EXISTING = "Add an existing task"
_ADD_NEW = "Create a new task"
_NO_MORE_TASKS = "Done"

_TASK_HINTS = {
    _KEEP: "Keep the task as it is.",
    _EDIT: "Go through the task-creation flow, prefilled with the task. Its task file is "
    "rewritten once the course is done.",
    _REMOVE: "Remove the task from the course. Its task file is kept.",
}

# A task of the course: its file path, and for tasks created or edited in the flow (not
# yet written), the keyword arguments for ``Task.create_task_json``.
_Entry = tuple[Path, dict[str, Any] | None]


class _CourseTask(NamedTuple):
    """A task of a course that is being edited."""

    path: Path
    # the task's name, or its name in the course manifest if its task file can't be read
    name: str
    # the task's fields (see ``Task.read_task_json``), None if its task file can't be read
    fields: dict[str, Any] | None
    # names the task can't be renamed to: those of the other tasks in other courses that
    # include it
    taken_elsewhere: frozenset[str] = frozenset()
    # why the task file can't be read
    problem: str = ""


def _course_tasks(fp: Path, manifest: Mapping[str, Any]) -> dict[str, Path]:
    """The tasks of the course ``manifest`` read from ``fp``, by name, with their task
    files' absolute paths."""
    base = Path(fp).parent
    return {
        k: paths.absolute(paths.resolve(v, base)) for k, v in manifest["tasks"].items()
    }


def _names_taken_elsewhere(
    task_file: PathLike, courses: Mapping[Path, Mapping[str, Any]]
) -> frozenset[str]:
    """The names of the other tasks in the ``courses`` (see ``find_courses``) that
    include ``task_file``, which the task can't be renamed to."""
    task = paths.absolute(task_file)
    taken: set[str] = set()
    for fp, manifest in courses.items():
        tasks = _course_tasks(fp, manifest)
        if task in tasks.values():
            taken.update(k for k, v in tasks.items() if v != task)
    return frozenset(taken)


def _rename_in_courses(
    task_file: PathLike, name: str, courses: dict[Path, dict[str, Any]]
) -> list[Path]:
    """
    Rename the task ``task_file`` to ``name`` in the ``courses`` (see ``find_courses``)
    that include it under another name, keeping its position in them, and rewrite their
    manifests. ``courses`` is updated to match.

    Returns
    -------
    list[Path]
        The rewritten manifests.
    """
    task = paths.absolute(task_file)
    renamed: list[Path] = []
    for fp, manifest in courses.items():
        tasks = _course_tasks(fp, manifest)
        if not any(v == task and k != name for k, v in tasks.items()):
            continue
        manifest["tasks"] = {
            name if tasks[k] == task else k: v for k, v in manifest["tasks"].items()
        }
        # serialized before the file is opened, so it isn't left empty if that fails
        text = json.dumps(manifest, indent=4)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        renamed.append(fp)
    return renamed


def _prompt_task_file(
    s: PromptSession, found: dict[Path, str], tasks: dict[str, _Entry]
) -> Path:
    """Ask for one of the ``found`` task files that isn't in the course yet. Its task name
    must be new to the course, too."""
    selected = {paths.absolute(p) for p, _ in tasks.values()}
    labels = {
        f"{n} ({p})": p for p, n in found.items() if paths.absolute(p) not in selected
    }

    def check(label: str) -> Path:
        _check_name(found[labels[label]], tasks)
        return labels[label]

    return s.ask(SelectInput("Task file", list(labels)), check)


def _check_path(raw: str, tasks: dict[str, _Entry]) -> Path:
    """Return ``raw`` as a path if a new task file can be written there, raise
    otherwise."""
    path = Path(raw)
    if not path.name or path.is_dir():
        raise ValueError(f"{raw!r} is not a file path")
    if paths.absolute(path) in {paths.absolute(p) for p, _ in tasks.values()}:
        raise ValueError(f"the course already has a task at {raw!r}")
    return path


def _prompt_tasks(
    s: PromptSession, found: dict[Path, str], current: list[_CourseTask] | None = None
) -> dict[str, _Entry]:
    """
    Interactively collect at least one task, each either one of the ``found`` task files
    or created on the fly with the task-creation flow. When editing a course, its
    ``current`` tasks are kept, edited or removed first, and adding more is not
    preselected.

    Returns
    -------
    dict[str, _Entry]
        The tasks by name, in the order they were added.
    """
    tasks: dict[str, _Entry] = {}
    for i, task in enumerate(current or [], 1):
        s.cli.update_progress(1 / _TOTAL_STEPS)

        options, problem = [_KEEP, _EDIT, _REMOVE], task.problem
        if task.fields is None:
            options = [_REMOVE]
        elif task.name in tasks:
            options = [_EDIT, _REMOVE]
            problem = (
                f"Another task of the course is named {task.name!r}, so the task needs "
                "to be renamed or removed."
            )
        lines = [Text(problem, style="red")] if problem else []
        lines.append(Text(str(task.path), style="dim"))

        label = f"Task {i} of {len(current or [])}: {escape(task.name)}"
        details = Group(*lines)
        choice = s.ask(SelectInput(label, options, hints=_TASK_HINTS, details=details))
        if choice == _REMOVE:
            continue

        fields = None
        if choice == _EDIT:
            # not the name of another task of the course, including those still to come
            # (which are most likely kept), nor of one in another course with the task
            taken = {*tasks, *(t.name for t in (current or [])[i:])}
            taken |= task.taken_elsewhere
            fields = _task_flow(s, taken_names=taken, current=task.fields)
        tasks[fields["name"] if fields else task.name] = (task.path, fields)

    hints = {
        _ADD_EXISTING: "Pick one of the task files found below the working directory.",
        _ADD_NEW: "Define a new task, written to a task file once the course is done.",
    }
    while True:
        # the task-creation flow moves the progress bar through its own steps
        s.cli.update_progress(1 / _TOTAL_STEPS)

        # offered only while some found task file isn't in the course yet
        selected = {paths.absolute(p) for p, _ in tasks.values()}
        unselected = any(paths.absolute(p) not in selected for p in found)
        options = [_ADD_EXISTING] if unselected else []
        options.append(_ADD_NEW)
        if tasks:  # a course can't be empty
            options.append(_NO_MORE_TASKS)
        hints[_NO_MORE_TASKS] = f"Finish adding tasks ({len(tasks)} added so far)."

        prompt = SelectInput("How do you want to add a task?", options, hints=hints)
        if current is not None:
            prompt.set_answer(_NO_MORE_TASKS)
        kind = s.ask(prompt)
        if kind == _NO_MORE_TASKS:
            return tasks

        if kind == _ADD_EXISTING:
            path = _prompt_task_file(s, found, tasks)
            tasks[found[path]] = (path, None)
            continue

        fields = _task_flow(s, taken_names=tasks)
        prompt = PathInput(
            "Task file path", initial=f"./{fields['name']}{TASK_SUFFIX}"
        )
        tasks[fields["name"]] = (s.ask(prompt, lambda p: _check_path(p, tasks)), fields)


def _course_flow(
    s: PromptSession,
    found: dict[Path, str],
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The full course-creation flow. Replayed by ``PromptSession`` whenever the user goes
    back, so it must ask the same prompts given the same answers, and must not write any
    files.

    With ``current``, the ``name`` and ``tasks`` (as ``_CourseTask``) of an existing
    course, the course is edited instead: every prompt is prefilled with its current
    value, so that confirming all of them leaves the course as it was.

    Returns
    -------
    dict[str, Any]
        The course ``name``, and its ``tasks`` as ``_Entry`` tuples, in course order.
    """
    s.cli.update_progress(0 / _TOTAL_STEPS)
    prompt = TextInput("Course name", initial=current["name"] if current else "")
    name = s.ask(prompt, lambda n: _check_name(n, ()))

    tasks = _prompt_tasks(s, found, current["tasks"] if current else None)

    s.cli.update_progress(2 / _TOTAL_STEPS)
    order = list(tasks)
    if len(order) > 1:
        order = s.ask(ReorderInput("Task order", order))

    s.cli.update_progress(3 / _TOTAL_STEPS)
    return {"name": name, "tasks": [tasks[n] for n in order]}


def _create_course(fp: PathLike | None = None, max_depth: int = 3) -> None:
    """
    Console script to collect task files and construct a course manifest from them.
    Tasks are either picked from the task files below the working directory, or created
    on the fly.

    Parameters
    ----------
    fp: PathLike
        Output path for the resulting ``.pyicscourse.json`` file.
    max_depth: int
        How many directory levels below the working directory are searched for task
        files, so the search stays short when run in e.g. the home directory.
    """
    with fullscreen():
        cli = CLI()
        cli()
        cli.update_info("Searching for task files...")

        try:
            # searched once up front, since the flow is replayed whenever the user
            # goes back
            found = find_task_files(Path.cwd(), max_depth)
            cli.update_info(_INFO)

            with KeyListener() as q:
                fields = PromptSession(cli, q, info=_INFO).run(
                    lambda s: _course_flow(s, found)
                )

            task_files: list[PathLike] = []
            for path, task_fields in fields["tasks"]:
                if task_fields is not None:
                    path = Task.create_task_json(path, **task_fields)
                task_files.append(path.absolute())

            fp = Course.create_course_manifest(
                fp or Path(f"./{fields['name']}"), task_files, name=fields["name"]
            )
            cli.update_info(f"Course written to {escape(str(fp))}")
        except PromptCancelled:
            cli.update_info("Cancelled, no course was created.")
        finally:
            cli.stop()
