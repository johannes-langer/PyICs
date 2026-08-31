import json
import queue
import threading
import time
from typing import Any

from jsonargparse import Namespace

from pyics.actions import Action, KeyAction, Demo, Quit
from pyics.cli import CLI
from pyics.key_listener import hey_listen


class PyIC:
    """PyIC main thread"""

    def __init__(self, cli: CLI):
        self.cli = cli
        self.running = False
        self.key_actions: dict[str, KeyAction] = {}

        self._register_actions()
        self.cli.update_hotkeys({a.ref: a.name for a in self.key_actions.values()})

    def _register_actions(self) -> None:
        """ """
        key_actions: list[KeyAction] = [Quit(cli=self.cli, pyics=self), Demo(cli=self.cli)]
        for a in key_actions:
            self.key_actions[a.key] = a

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

            self.key_actions.get(key, lambda: ...)()

        except queue.Empty:
            pass
