import itertools
from pathlib import Path
import queue
import threading
import time
from typing import Any

from rich.markup import escape
from rich.text import Text


from pyics.app.actions import KeyAction, Next, Previous, Quit, ScrollDown, ScrollUp
from pyics.app.cli import CLI
from pyics.app.key_listener import hey_listen
from pyics.data import Task, Course


class PyIC:
    """PyIC main thread"""

    __current_course: Course | None

    def __init__(self, cli: CLI):
        self.cwd = Path.cwd()
        self.cli = cli
        self.running = False
        self.key_actions: dict[str, KeyAction] = {}
        self.__current_course: Course | None = None

        self._register_actions()
        self.cli.update_hotkeys({a.ref: a.name for a in self.key_actions.values()})

    def _find_course(self, max_depth: int = 2) -> None:
        """TODO: This is indev behavior. In the future, there should be a course selector
        if the application is not called with a specific course selected

        Search the current working directory for course json files (expecting only one)
        and select it.

        Parameters
        ----------
        max_depth: int (optional)
            Set the maximum depth to walk the directory. For example, with the default
            value (2), this method would find ``./subdir/subsubdir/task.json``, but not
            ``./subdir/subsubdir/subsubsubdir/task.json``.
        """
        globs = [
            self.cwd.glob(f"{'*/' * d}*.pyicscourse.json") for d in range(max_depth + 1)
        ]
        self.__current_course = Course.load_course_manifest(
            sorted(itertools.chain(*globs))[0]
        )
        self.cli.update_info(escape(self.__current_course.current_task.instructions))

    def _register_actions(self) -> None:
        """ """
        key_actions: list[KeyAction] = [
            Quit(cli=self.cli, pyics=self),
            Next(pyics=self),
            Previous(pyics=self),
            ScrollUp(cli=self.cli, pyics=self),
            ScrollDown(cli=self.cli, pyics=self),
        ]
        for a in key_actions:
            self.key_actions[a.key] = a

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """ """
        self.running = True
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    @property
    def current_course(self) -> Course | None:
        """"""
        return self.__current_course

    def next_task(self) -> None:
        """
        Choose the next task in the self.tasks iteratable. Specifically load the task now
        to avoid populating memory with fully loaded tasks.
        """
        if self.current_course is not None:
            self.current_course.next()

    @hey_listen("q")
    def run(self, q: queue.Queue[str]):
        """ """
        self.cli()
        self._find_course()

        while self.running:
            self.check_keypress(q)
            if self.current_course is not None:
                new_test_results, new_progress = self.current_course.tick()
                if new_test_results:
                    self.cli.update_main_content(
                        self.current_course.current_task.status,
                        Text(
                            self.current_course.current_task.exercise_files[0].as_posix(),
                            style="dim",
                        ),
                    )
                    self.current_course.current_task.clear_test_results()
                if new_progress:
                    self.cli.update_progress(float(self.current_course.progress))

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
