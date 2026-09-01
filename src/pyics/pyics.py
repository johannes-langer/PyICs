import itertools
from pathlib import Path
import queue
import threading
import time
from typing import Any


from pyics.actions import Demo, KeyAction, Quit
from pyics.cli import CLI
from pyics.key_listener import hey_listen
from pyics.task import Task


class PyIC:
    """PyIC main thread"""

    __current_task: Task | None

    def __init__(self, cli: CLI):
        self.cwd = Path.cwd()
        self.cli = cli
        self.running = False
        self.key_actions: dict[str, KeyAction] = {}
        self.__current_task: Task | None = None
        self.tasks: list[Path] = []

        self._find_tasks()

        self._register_actions()
        self.cli.update_hotkeys({a.ref: a.name for a in self.key_actions.values()})

    def _find_tasks(self, max_depth: int = 2) -> None:
        """TODO: This is indev behavior. Future application should have courses composed
        of tasks. If anything, top-level should find courses (see Obsidian Notes).

        Search the current working directory for task json files.

        Parameters
        ----------
        max_depth: int (optional)
            Set the maximum depth to walk the directory. For example, with the default
            value (2), this method would find ``./subdir/subsubdir/task.json``, but not
            ``./subdir/subsubdir/subsubsubdir/task.json``.
        """
        globs = [self.cwd.glob(f"{'*/' * d}*.pyicstask.json") for d in range(max_depth + 1)]
        self.tasks = sorted(itertools.chain(*globs))

    def _register_actions(self) -> None:
        """ """
        key_actions: list[KeyAction] = [
            Quit(cli=self.cli, pyics=self),
            Demo(cli=self.cli, pyics=self),
        ]
        for a in key_actions:
            self.key_actions[a.key] = a

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """ """
        self.running = True
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    @property
    def current_task(self) -> Task | None:
        """
        TODO: In the future, this should point to the current task of the active course.
        Ensure this works dynamically.
        """
        return self.__current_task

    def next_task(self) -> None:
        """
        Choose the next task in the self.tasks iteratable. Specifically load the task now
        to avoid populating memory with fully loaded tasks.
        """
        if self.current_task is None:
            self.__current_task = Task(self.tasks[0])
        else:
            self.__current_task = Task(
                self.tasks[self.tasks.index(Path(self.current_task.path)) + 1]
            )

    @hey_listen("q")
    def run(self, q: queue.Queue[str]):
        """ """
        self.cli()

        while self.running:
            self.check_keypress(q)
            if self.current_task is not None and self.current_task.check_file_changes():
                self.current_task.run_tests()
            if self.current_task is not None and self.current_task.new_test_results:
                self.cli.update_main_content(self.current_task.status)
                self.current_task.clear_test_results()

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
