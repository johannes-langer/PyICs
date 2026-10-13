import sys
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

from rich.markup import escape

import pyics.util.json_handling as json
from pyics.app.cli import CLI
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.data.task import Task
from pyics.distribution import archive
from pyics.scripts.console import warn_differing_notebooks
from pyics.scripts.create_course import (
    _course_tasks,
    _names_taken_elsewhere,
    _rename_in_courses,
)
from pyics.scripts.create_task import _task_flow
from pyics.util import paths
from pyics.util.discovery import TASK_SUFFIX, find_courses
from pyics.util.terminal import fullscreen


def _differing_notebooks(
    task_file: PathLike, courses: Mapping[Path, Mapping[str, Any]]
) -> dict[Path, list[Path]]:
    """The exercise notebooks of the task ``task_file`` that other tasks of the
    ``courses`` (see ``find_courses``) that include it share, but store different
    contents for (see ``archive.differing_notebooks``)."""
    task = paths.absolute(task_file)
    tasks: dict[Path, dict[str, Any]] = {}
    for fp, manifest in courses.items():
        task_files = _course_tasks(fp, manifest).values()
        if task not in task_files:
            continue
        for path in task_files:
            try:
                data = tasks.get(path) or json.load_plain(path)
            except Exception:  # noqa: BLE001
                continue  # its course can't be loaded either
            if isinstance(data, dict):
                tasks[path] = data
    return {
        notebook: sharing
        for notebook, sharing in archive.differing_notebooks(tasks).items()
        if task in sharing
    }


def _edit_task(task_file: PathLike, max_depth: int = 3) -> None:
    """
    Console script to interactively edit a task file, going through the task-creation
    flow with every prompt prefilled from the task. The task file is rewritten in place.

    The course manifests below the working directory that include the task are kept
    consistent with it: if the task is renamed, so is its entry in them, and its new
    name can't be that of another task in one of them. If other tasks in them share a
    notebook with the task, but store different contents for it, that is warned about.

    Parameters
    ----------
    task_file: PathLike
        The ``.pyicstask.json`` file to edit.
    max_depth: int
        How many directory levels below the working directory are searched for course
        manifests, so the search stays short when run in e.g. the home directory.
    """
    task_file = Path(task_file)
    if not task_file.name.endswith(TASK_SUFFIX):
        sys.exit(f"error: {task_file} is not a task file (*{TASK_SUFFIX})")
    try:
        current = Task.read_task_json(task_file)
    except Exception as err:  # noqa: BLE001
        sys.exit(f"error: can't read the task file {task_file}: {err}")

    with fullscreen():
        cli = CLI()
        cli()
        cli.update_info("Searching for course manifests...")

        try:
            # searched once up front, since the flow is replayed whenever the user
            # goes back
            courses = find_courses(Path.cwd(), max_depth)
            taken = _names_taken_elsewhere(task_file, courses)
            info = f"Editing the task {escape(str(current['name']))}..."
            cli.update_info(info)

            with KeyListener() as q:
                fields = PromptSession(cli, q, info=info).run(
                    lambda s: _task_flow(s, taken_names=taken, current=current)
                )

            Task.create_task_json(task_file, **fields)
            message = f"Task written to {task_file}"
            if renamed := _rename_in_courses(task_file, fields["name"], courses):
                message += f", and renamed in {', '.join(map(str, renamed))}"
            differing = _differing_notebooks(task_file, courses)
        except PromptCancelled:
            message = "Cancelled, the task was not changed."
            differing = {}
        finally:
            cli.stop()
    # after leaving the alternate screen, so that it stays visible
    print(message)
    warn_differing_notebooks(differing, Path.cwd())
