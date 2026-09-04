from os import PathLike
from pathlib import Path
import sys

import jsonargparse

from pyics.app.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI
from pyics.util.logging import setup_logging
from pyics.app.pyics import PyIC


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
    cli.update_progress(0.3)

    pyics = PyIC(cli)

    try:
        pyics()
    finally:
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


def run():
    jsonargparse.auto_cli(main)


if __name__ == "__main__":
    run()
