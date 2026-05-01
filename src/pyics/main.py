import queue
import sys
import time

from pyics.cli import ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, CLI
from pyics.key_listener import hey_listen


def launch():

    sys.stdout.write(ALT_SCREEN_ENTER)
    sys.stdout.flush()

    cli = CLI()
    cli.update_progress(0.3)

    try:
        cli()
        main(cli)  # pyright: ignore[reportCallIssue]

    finally:
        cli.running = False
        time.sleep(0.05)
        sys.stdout.write(ALT_SCREEN_LEAVE)
        sys.stdout.flush()


@hey_listen("q")
def main(cli: CLI, q: queue.Queue):
    """Simulated, will be OO-solution later"""
    while True:
        try:
            key = q.get_nowait()

            match key:
                case "\x03":
                    cli.update_main_content("[dim]Stopping...")
                    break
                case "n":
                    cli.update_main_content("[dim]Pressed key to go to next task.")
                case "p":
                    cli.update_main_content("[dim]Pressed key to go to previous task.")
                case "s":
                    cli.update_main_content("[dim]Pressed key to skip task.")
                case _:
                    ...

        except queue.Empty:
            pass

        time.sleep(0.05)


if __name__ == "__main__":
    launch()
