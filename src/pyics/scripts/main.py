import inspect
from os import PathLike
from pathlib import Path

import jsonargparse
from jsonargparse import ActionYesNo

from pyics.app.cli import CLI
from pyics.util.logging import setup_logging
from pyics.util.terminal import fullscreen
from pyics.app.pyics import PyIC
from pyics.scripts.console import check_legacy_sources
from pyics.scripts.create_course import _create_course
from pyics.scripts.create_task import _create_task
from pyics.scripts.edit_course import _edit_course
from pyics.scripts.edit_task import _edit_task
from pyics.scripts.pack import _build_index, _pack
from pyics.scripts.pull import _pull, _update
from pyics.scripts.sources import _add_source, _remove_source, _search, _sources


def main(*, log_level: str | int = 50, log_file: PathLike = Path("./.pyics.log")):
    # This is necessary because otherwise pytest.main() imports it and then subsequently
    # breaks our shutdown mechanic by taking control of tty.
    try:
        import readline
    except ImportError:
        pass

    setup_logging(log_level, log_file)

    with fullscreen():
        cli = CLI()

        pyics = PyIC(cli)

        try:
            pyics()
            pyics.thread.join()
        finally:
            cli.stop()
    # after leaving the alternate screen, so that it stays visible
    if pyics.exit_message:
        print(pyics.exit_message)


# Subcommands of the ``pyics`` script, run as e.g. ``pyics create_task``. Without one,
# the app itself is run.
_SUBCOMMANDS = {
    "create_task": (_create_task, "Interactively create a task file."),
    "create_course": (_create_course, "Interactively create a course manifest."),
    "edit_task": (_edit_task, "Interactively edit a task file."),
    "edit_course": (_edit_course, "Interactively edit a course manifest."),
    "add_source": (_add_source, "Add a course library by the URL of its index."),
    "remove_source": (_remove_source, "Remove a course library."),
    "sources": (_sources, "List the course libraries."),
    "search": (_search, "Find courses in the course libraries."),
    "pull": (_pull, "Pull a course by its name, or a course archive by URL or path."),
    "update": (_update, "Update a pulled course, keeping your work."),
    "pack": (_pack, "Pack a course into an archive, to distribute it."),
    "build_index": (_build_index, "Write the index of a course library."),
}


# parameters of subcommands that are given as optional positional arguments, e.g.
# ``pyics search bfs`` rather than ``pyics search --term bfs``
_OPTIONAL_POSITIONALS = {"term", "directory"}


def _add_subcommand_arguments(parser: jsonargparse.ArgumentParser, fn) -> None:
    """
    Add the parameters of ``fn`` to ``parser``: those without a default, and those of
    ``_OPTIONAL_POSITIONALS``, as positional arguments, boolean ones as flags, e.g.
    ``--yes`` (and ``--no_yes``) rather than ``--yes true``, and lists of strings as
    options that take one or more values, e.g. ``--tags a b`` or ``--tags a --tags b``.
    """
    params = inspect.signature(fn).parameters
    flags = {name for name, param in params.items() if param.annotation is bool}
    lists = {
        name
        for name, param in params.items()
        if param.annotation in (list[str], list[str] | None)
    }
    positionals = _OPTIONAL_POSITIONALS & set(params)
    parser.add_function_arguments(
        fn, as_group=False, as_positional=True, skip=flags | lists | positionals
    )
    for name, param in params.items():
        if name in positionals:
            parser.add_argument(
                name, nargs="?", type=param.annotation, default=param.default
            )
        elif name in flags:
            parser.add_argument(f"--{name}", action=ActionYesNo, default=param.default)
        elif name in lists:
            parser.add_argument(
                f"--{name}", nargs="+", action="extend", type=str, default=param.default
            )


def run():
    parser = jsonargparse.ArgumentParser(prog="pyics")
    parser.add_function_arguments(main, as_group=False)
    subcommands = parser.add_subcommands(required=False)
    for name, (fn, help) in _SUBCOMMANDS.items():
        subparser = jsonargparse.ArgumentParser()
        _add_subcommand_arguments(subparser, fn)
        subcommands.add_subcommand(name, subparser, help=help)

    cfg = parser.parse_args()
    # before the fullscreen interface, which can't ask plain questions
    check_legacy_sources()
    if cfg.subcommand is None:
        main(log_level=cfg.log_level, log_file=cfg.log_file)
    else:
        fn, _ = _SUBCOMMANDS[cfg.subcommand]
        fn(**cfg[cfg.subcommand].as_dict())


if __name__ == "__main__":
    run()
