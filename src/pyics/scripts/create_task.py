import jsonargparse

from pyics.app.cli import CLI


def _create_task() -> None:
    """
    Console script to interactively create a task file.

    TODO
    ----
    - Can this make use of CLI to have a consistent visual experience?
    - do not have the main loop, but instead have the main thread being blocked by
      input requests
    """
    cli = CLI()

    cli()
    cli.update_info("Creating a new task...")


def create_task() -> None:
    jsonargparse.auto_cli(_create_task)
