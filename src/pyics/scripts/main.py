from os import PathLike
from pathlib import Path
import sys

import jsonargparse

from pyics.app.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI
from pyics.util.logging import setup_logging
from pyics.app.pyics import PyIC
from pyics.scripts.create_course import _create_course
from pyics.scripts.create_task import _create_task
from pyics.scripts.edit_course import _edit_course
from pyics.scripts.edit_task import _edit_task


def main(*, log_level: str | int = 50, log_file: PathLike = Path("./.pyics.log")):
    # This is necessary because otherwise pytest.main() imports it and then subsequently
    # breaks our shutdown mechanic by taking control of tty.
    try:
        import readline
    except ImportError:
        pass

    setup_logging(log_level, log_file)

    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()

    cli = CLI()

    pyics = PyIC(cli)

    try:
        pyics()
    finally:
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


# Subcommands of the ``pyics`` script, run as e.g. ``pyics create_task``. Without one,
# the app itself is run.
_SUBCOMMANDS = {
    "create_task": (_create_task, "Interactively create a task file."),
    "create_course": (_create_course, "Interactively create a course manifest."),
    "edit_task": (_edit_task, "Interactively edit a task file."),
    "edit_course": (_edit_course, "Interactively edit a course manifest."),
}


def run():
    parser = jsonargparse.ArgumentParser(prog="pyics")
    parser.add_function_arguments(main, as_group=False)
    subcommands = parser.add_subcommands(required=False)
    for name, (fn, help) in _SUBCOMMANDS.items():
        subparser = jsonargparse.ArgumentParser()
        subparser.add_function_arguments(fn, as_group=False, as_positional=True)
        subcommands.add_subcommand(name, subparser, help=help)

    cfg = parser.parse_args()
    if cfg.subcommand is None:
        main(log_level=cfg.log_level, log_file=cfg.log_file)
    else:
        fn, _ = _SUBCOMMANDS[cfg.subcommand]
        fn(**cfg[cfg.subcommand].as_dict())


if __name__ == "__main__":
    run()
