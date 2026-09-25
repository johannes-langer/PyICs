import os
import sys
from os import PathLike
from pathlib import Path
from typing import Any

import jsonargparse
from rich.markup import escape

import pyics.util.json_handling as json
from pyics.app.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI, SHOW_CURSOR
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
from pyics.scripts.create_task import _check_name, _task_flow
from pyics.util.logging import get_logger

_TOTAL_STEPS = 3
_INFO = "Creating a new course..."

_TASK_SUFFIX = ".pyicstask.json"
# never searched for task files: large, and never contain any
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}

_ADD_EXISTING = "Add an existing task"
_ADD_NEW = "Create a new task"
_NO_MORE_TASKS = "Done"

# A task of the course: its file path, and for tasks created in the flow (not yet
# written), the keyword arguments for ``Task.create_task_json``.
_Entry = tuple[Path, dict[str, Any] | None]


def _find_task_files(root: Path, max_depth: int) -> dict[Path, str]:
    """
    Find task files at most ``max_depth`` directories below ``root``, skipping
    ``_SKIP_DIRS``. Files that can't be read or have no task name are skipped.

    Returns
    -------
    dict[Path, str]
        The task files' paths, relative to ``root``, mapped to their task names.
    """
    found: dict[Path, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) >= max_depth:
            dirnames.clear()
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(_TASK_SUFFIX):
                continue
            path = Path(dirpath, filename)
            try:
                with open(path) as f:
                    name = json.load(f)["name"]
            except Exception:  # noqa: BLE001
                get_logger(__name__).debug(f"Skipping {path}", exc_info=True)
                continue
            if isinstance(name, str):
                found[rel / filename] = name
    return found


def _prompt_task_file(
    s: PromptSession, found: dict[Path, str], tasks: dict[str, _Entry]
) -> Path:
    """Ask for one of the ``found`` task files that isn't in the course yet. Its task name
    must be new to the course, too."""
    selected = {p for p, _ in tasks.values()}
    labels = {f"{n} ({p})": p for p, n in found.items() if p not in selected}

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
    if path.absolute() in {p.absolute() for p, _ in tasks.values()}:
        raise ValueError(f"the course already has a task at {raw!r}")
    return path


def _prompt_tasks(s: PromptSession, found: dict[Path, str]) -> dict[str, _Entry]:
    """
    Interactively collect at least one task, each either one of the ``found`` task files
    or created on the fly with the task-creation flow.

    Returns
    -------
    dict[str, _Entry]
        The tasks by name, in the order they were added.
    """
    tasks: dict[str, _Entry] = {}
    hints = {
        _ADD_EXISTING: "Pick one of the task files found below the working directory.",
        _ADD_NEW: "Define a new task, written to a task file once the course is done.",
    }
    while True:
        # the task-creation flow moves the progress bar through its own steps
        s.cli.update_progress(1 / _TOTAL_STEPS)

        # offered only while some found task file isn't in the course yet
        selected = {p for p, _ in tasks.values()}
        options = [_ADD_EXISTING] if found.keys() - selected else []
        options.append(_ADD_NEW)
        if tasks:  # a course can't be empty
            options.append(_NO_MORE_TASKS)
        hints[_NO_MORE_TASKS] = f"Finish adding tasks ({len(tasks)} added so far)."

        kind = s.ask(
            SelectInput("How do you want to add a task?", options, hints=hints)
        )
        if kind == _NO_MORE_TASKS:
            return tasks

        if kind == _ADD_EXISTING:
            path = _prompt_task_file(s, found, tasks)
            tasks[found[path]] = (path, None)
            continue

        fields = _task_flow(s, taken_names=tasks)
        prompt = PathInput(
            "Task file path", initial=f"./{fields['name']}{_TASK_SUFFIX}"
        )
        tasks[fields["name"]] = (s.ask(prompt, lambda p: _check_path(p, tasks)), fields)


def _course_flow(s: PromptSession, found: dict[Path, str]) -> dict[str, Any]:
    """
    The full course-creation flow. Replayed by ``PromptSession`` whenever the user goes
    back, so it must ask the same prompts given the same answers, and must not write any
    files.

    Returns
    -------
    dict[str, Any]
        The course ``name``, and its ``tasks`` as ``_Entry`` tuples, in course order.
    """
    s.cli.update_progress(0 / _TOTAL_STEPS)
    name = s.ask(TextInput("Course name"), lambda n: _check_name(n, ()))

    tasks = _prompt_tasks(s, found)

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
    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()

    cli = CLI()
    cli()
    cli.update_info("Searching for task files...")

    try:
        # searched once up front, since the flow is replayed whenever the user goes back
        found = _find_task_files(Path.cwd(), max_depth)
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
        cli.running = False
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


def create_course() -> None:
    jsonargparse.auto_cli(_create_course)
