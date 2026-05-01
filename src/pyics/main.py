import sys

from .cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI


def main():

    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()

    try:
        cli = CLI()
        cli.update_progress(0.3)
        cli()

    finally:
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
