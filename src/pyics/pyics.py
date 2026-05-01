import json
import queue
import threading
import time
from typing import Any, Callable

from jsonargparse import Namespace

from pyics.actions import Action, Demo, Quit
from pyics.cli import CLI
from pyics.key_listener import hey_listen


class PyICs:
    """PyICs main thread"""

    def __init__(self, cli: CLI):
        self.cli = cli
        self.running = False
        self.actions: dict[str, Action] = {}

        self._register_actions()
        self.cli.update_hotkeys({a.ref: a.name for a in self.actions.values()})

    def _register_actions(self) -> None:
        """ """
        actions: list[Action] = [Quit(cli=self.cli, pyics=self), Demo(cli=self.cli)]
        for a in actions:
            self.actions[a.key] = a

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """ """
        self.running = True
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    @hey_listen("q")
    def run(self, q: queue.Queue[str]):
        """ """
        self.cli()

        while self.running:
            self.check_keypress(q)

            time.sleep(0.05)

    def check_keypress(self, q: queue.Queue[str]) -> None:
        """
        Check if a key has been pressed, and if so, try to match it to known actions.

        Parameters
        ----------
        q: Queue
            The queue which collects key presses
        """
        try:
            key = q.get_nowait()

            self.actions.get(key, lambda: ...)()

        except queue.Empty:
            pass
