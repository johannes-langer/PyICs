import queue
import sys
import time

from pyics.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI
from pyics.pyics import PyIC


def main():

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


if __name__ == "__main__":
    main()
