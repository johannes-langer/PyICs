from os import PathLike
import sys
from pathlib import Path

import jsonargparse

from pyics.app.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI, SHOW_CURSOR
from pyics.app.key_listener import KeyListener
from pyics.app.prompts import PromptCancelled, prompt_select, prompt_text
from pyics.data.task import Task

_TOTAL_STEPS = 4


def _prompt_files(cli: CLI, q, kind: str) -> list[PathLike]:
    """
    Interactively collect a list of file paths, one at a time.

    Parameters
    ----------
    cli: CLI
        CLI instance to render prompts into.
    q: queue.Queue[str]
        Shared KeyListener queue for the whole script.
    kind: str
        Label for what's being collected (e.g. "exercise" or "solution"), used in prompts.
    """
    files: list[PathLike] = []
    while True:
        files.append(Path(prompt_text(cli, f"{kind.capitalize()} file path", q=q)))
        again = prompt_select(cli, f"Add another {kind} file?", ["yes", "no"], q=q)
        if again == "no":
            return files


def _create_task(fp: PathLike = Path("./new_task")) -> None:
    """
    Console script to interactively create a task file.

    Parameters
    ----------
    fp: PathLike
        Output path for the resulting ``.pyicstask.json`` file.
    """
    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()

    cli = CLI()
    cli()
    cli.update_info("Creating a new task...")

    try:
        with KeyListener() as q:
            cli.update_progress(0 / _TOTAL_STEPS)
            name = prompt_text(cli, "Task name", q=q)

            cli.update_progress(1 / _TOTAL_STEPS)
            instructions = prompt_text(cli, "Instructions", multiline=True, q=q)

            cli.update_progress(2 / _TOTAL_STEPS)
            ex_files = _prompt_files(cli, q, "exercise")

            cli.update_progress(3 / _TOTAL_STEPS)
            sol_files = _prompt_files(cli, q, "solution")

            cli.update_progress(4 / _TOTAL_STEPS)

        # TODO: also collect test definitions here. Needs its own design -- test fields
        # (e.g. SimpleTest's function_args/function_kwargs/expected) are arbitrary Python
        # values, and some Test subclasses (StructureTest, SizeTest) aren't implemented
        # yet. For now, tasks are created without tests.
        Task.create_task_json(
            fp, name, instructions, ex_files=ex_files, sol_files=sol_files, tests=None
        )
        cli.update_info(f"Task written to {fp}")
    except PromptCancelled:
        cli.update_info("Cancelled, no task was created.")
    finally:
        cli.running = False
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


def create_task() -> None:
    jsonargparse.auto_cli(_create_task)
