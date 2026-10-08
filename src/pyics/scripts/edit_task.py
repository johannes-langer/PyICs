import sys
from os import PathLike
from pathlib import Path

from rich.markup import escape

from pyics.app.cli import CLI
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import PromptCancelled, PromptSession
from pyics.data.task import Task
from pyics.scripts.create_course import (
    _names_taken_elsewhere,
    _rename_in_courses,
)
from pyics.scripts.create_task import _task_flow
from pyics.util.discovery import TASK_SUFFIX, find_courses
from pyics.util.terminal import fullscreen


def _edit_task(task_file: PathLike, max_depth: int = 3) -> None:
    """
    Console script to interactively edit a task file, going through the task-creation
    flow with every prompt prefilled from the task. The task file is rewritten in place.

    The course manifests below the working directory that include the task are kept
    consistent with it: if the task is renamed, so is its entry in them, and its new
    name can't be that of another task in one of them.

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
        except PromptCancelled:
            message = "Cancelled, the task was not changed."
        finally:
            cli.stop()
    # after leaving the alternate screen, so that it stays visible
    print(message)
