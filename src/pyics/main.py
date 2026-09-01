import queue
import sys
import time

from pyics.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI
from pyics.pyics import PyIC


def main():
    # This is necessary because otherwise pytest.main() imports it and then subsequently
    # breaks our shutdown mechanic by taking control of tty.
    try:
        import readline
    except ImportError:
        pass

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
