from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
import os
import threading
from pathlib import Path
from typing import Optional

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from pyics.data.task import Task
from pyics.util import json_handling as json
from pyics.util import paths
from pyics.util.paths import StrPath
from pyics.util.logging import get_logger


def _hints_view(hints: list[str], total: int) -> RenderableType:
    """The revealed ``hints`` of a task with ``total`` hints, numbered below a rule."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style="bright_yellow")
    grid.add_column()
    for i, hint in enumerate(hints, 1):
        grid.add_row(f"{i}.", Text(hint))
    rule = Rule(f"[bright_yellow]Hints {len(hints)}/{total}[/]", style="bright_yellow")
    return Padding(Group(rule, grid), pad=(0, 0, 1, 0))


class Course:
    """
    Course class. Handles creation and storage of courses, as well as safe task loading
    and keeping track of course progress.
    """

    name: str
    tasks: dict[str, StrPath]

    def __init__(
        self,
        *,
        name: str,
        tasks: dict[str, StrPath],
        progress: list[int],
        check_progress: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        name: str
            Name of the course.
        tasks: dict[str, StrPath]
            Ordered mapping of task names to task files.
        progress: list[int]
            Progress of each task (1 if solved), in the order of ``tasks``.
        check_progress: bool (optional, keyword only)
            If False (default), start right away on the first task that is unsolved
            according to ``progress``. If True, the progress is instead checked from the
            exercise files on disk, task by task in a background thread, and the course
            starts on the first task found unsolved (see ``loading``).
        """
        self.name = name
        self.tasks = tasks
        self.__tasks_list = list(self.tasks)
        self.__progress: list[int] = progress

        self.__current_task: Task | None = None
        self.__task_changed: bool = False
        self.__progress_changed: bool = False
        self.__error: Exception | None = None
        # how many hints have been revealed, by task name, so they stay revealed when a
        # task is left and visited again
        self.__hints_shown: dict[str, int] = {}

        # the position of the task whose progress is being checked
        self.__checking: int | None = 0 if check_progress else None
        self.__stop_check = threading.Event()
        self.__check_thread: threading.Thread | None = None

        if check_progress:
            self.__check_thread = threading.Thread(
                target=self.__check_progress, daemon=True
            )
            self.__check_thread.start()
        else:
            # when you boot up a course, always show the next unsolved task
            start = self.__progress.index(0) if 0 in self.__progress else -1
            self.__set_current_task(Task(self.tasks[self.__tasks_list[start]]))

    def __set_current_task(self, task: Task) -> None:
        self.__current_task = task
        self.__task_changed = True

    def __check_progress(self) -> None:
        """
        DO NOT CALL THIS METHOD DIRECTLY, IT IS RUN BY ``__init__``.

        Check every task's progress from its exercise files, in order, and start on the
        first unsolved task (the last task if all are solved) as soon as it is found.
        """
        last: Task | None = None
        for i, name in enumerate(self.__tasks_list):
            if self.__stop_check.is_set():
                break
            self.__checking = i
            try:
                last = Task(self.tasks[name], setup=False)
                solved = last.check_solved()
            except Exception as err:  # noqa: BLE001
                get_logger(__name__).warning(
                    f"Can't check the task {name!r} of {self.name!r}", exc_info=True
                )
                if self.__current_task is None:
                    self.__error = err
                    break
                continue

            if self.__progress[i] != int(solved):
                self.__progress[i] = int(solved)
                self.__progress_changed = True
            if self.__current_task is None and not solved:
                last.setup()
                self.__set_current_task(last)
        else:
            if self.__current_task is None and last is not None:
                last.setup()
                self.__set_current_task(last)
        self.__checking = None

    def stop_progress_check(self, timeout: float | None = None) -> None:
        """
        Stop checking the progress after the task that is being checked, and wait for
        that (at most ``timeout`` seconds).
        """
        self.__stop_check.set()
        if self.__check_thread is not None:
            self.__check_thread.join(timeout)

    @property
    def tracing(self) -> bool:
        """Whether the current task's tests are traced (see ``Task.tracing``). Off for
        each task that is moved to, and while the course is loading."""
        return self.__current_task is not None and self.__current_task.tracing

    def toggle_tracing(self) -> None:
        """Turn tracing the current task's tests on or off (see ``Task.toggle_tracing``),
        unless the course is loading."""
        if self.__current_task is not None:
            self.__current_task.toggle_tracing()

    @property
    def loading(self) -> bool:
        """Whether the course is still looking for the task to start on."""
        return self.__current_task is None

    @property
    def check_status(self) -> str:
        """What the progress check is doing, or "" once it is done."""
        if self.__checking is None:
            return ""
        return f"Checking your progress... (task {self.__checking + 1}/{len(self.tasks)})"

    @property
    def error(self) -> Exception | None:
        """Why the course can't be started, if its progress check failed before a task
        to start on was found."""
        return self.__error

    @property
    def progress(self) -> Fraction:
        return Fraction(sum(self.__progress), len(self.__progress))

    @property
    def instructions(self) -> str:
        if self.__current_task is None:
            return ""
        return self.__current_task.instructions

    def __current_task_name(self) -> str:
        """The current task's name in the course."""
        return self.__tasks_list[self.__current_task_number()]

    @property
    def hints(self) -> list[str]:
        """The current task's hints that have been revealed (see ``reveal_hint``)."""
        if self.__current_task is None:
            return []
        shown = self.__hints_shown.get(self.__current_task_name(), 0)
        return self.__current_task.help[:shown]

    def reveal_hint(self) -> bool:
        """
        Reveal the current task's next hint, if it has one that hasn't been revealed.

        Returns
        -------
        bool
            Whether a hint has been revealed. False while the course is loading.
        """
        if self.__current_task is None:
            return False
        name = self.__current_task_name()
        shown = self.__hints_shown.get(name, 0)
        if shown >= len(self.__current_task.help):
            return False
        self.__hints_shown[name] = shown + 1
        return True

    @property
    def info(self) -> RenderableType:
        """
        The current task's info, i.e. what is supposed to be displayed in the pyics header.
        """
        if self.__current_task is None:
            return ""
        return self.__current_task.info

    # @info.setter
    # def info(self, v: str) -> None:
    #     """
    #     I don't think this is needed.
    #     """
    #     self.__current_task.info = v

    @property
    def status(self) -> RenderableType:
        """
        The current task's status, i.e. what is supposed to be displayed in the pyics
        main panel, below its revealed hints.
        """
        if self.__current_task is None:
            return Text(self.check_status, style="dim")
        if hints := self.hints:
            total = len(self.__current_task.help)
            return Group(_hints_view(hints, total), self.__current_task.status)
        return self.__current_task.status

    def update_status(self) -> None:
        """
        Expose the current task's update_status() method.
        """
        if self.__current_task is not None:
            self.__current_task.update_status()

    @property
    def exercise_files(self) -> list[Path]:
        """
        The current task's exercise file paths.
        """
        if self.__current_task is None:
            return []
        return self.__current_task.exercise_files

    def __current_task_number(self) -> int:
        """The current task's position in the course."""
        assert self.__current_task is not None
        return self.__tasks_list.index(
            {v: k for k, v in self.tasks.items()}[self.__current_task.path]
        )

    def next(self) -> Task | None:
        """
        Select the next task. If the current task is unsolved, returns the current task.

        Returns
        -------
        Task | None
            Next task if the current task is solved, otherwise current task. None while
            the course is loading.
        """
        if self.__current_task is not None and self.__current_task.solved:
            current_task_number = self.__current_task_number()

            if current_task_number + 1 < len(self.__tasks_list):
                self.__set_current_task(
                    Task(self.tasks[self.__tasks_list[current_task_number + 1]])
                )

        return self.__current_task

    def previous(self) -> Task | None:
        """
        Select the previous task (if available)

        Returns
        -------
        Task | None
            Previous task if the current task isn't the first task, otherwise current
            task. None while the course is loading.
        """
        if self.__current_task is None:
            return None

        current_task_number = self.__current_task_number()

        if current_task_number != 0:
            self.__set_current_task(
                Task(self.tasks[self.__tasks_list[current_task_number - 1]])
            )

        return self.__current_task

    @property
    def task_changed(self) -> bool:
        """
        Flag to indicate whether the current task has changed, set by next or previous,
        and when the course has started on a task.
        """
        return self.__task_changed

    def clear_task_changed(self) -> None:
        """Expose the option to reset task_changed"""
        self.__task_changed = False

    def clear_test_results(self) -> None:
        """Expose the current task's clear_test_results method"""
        if self.__current_task is not None:
            self.__current_task.clear_test_results()

    def tick(self) -> tuple[bool, bool]:
        """
        Perform regular actions on the course, or pass them on to the current task. Does
        the following:

        1. Calls check_file_changes on the current task
        2. Calls run_tests on the current task if files have changed.
        3. checks if the course progress needs to be updated

        Returns
        -------
        tuple[bool, bool]
            first position indicates whether new test results from the current task are
            available,
            second position indicates whether the course progress has been updated (i.e.
            whether a task has been newly solved/unsolved, here or by the progress check)
        """
        new_progress, self.__progress_changed = self.__progress_changed, False

        if self.__current_task is None:
            return (False, new_progress)

        files_changed = self.__current_task.check_file_changes()

        if files_changed:
            self.__current_task.run_tests()

        current_task_number = self.__current_task_number()
        solved = int(self.__current_task.solved)
        if self.__progress[current_task_number] != solved:
            self.__progress[current_task_number] = solved
            new_progress = True

        return (self.__current_task.new_test_results, new_progress)

    @classmethod
    def load_course_manifest(cls, fp: StrPath, *, check_progress: bool = False) -> Course:
        """
        Load a course manifest to a ``Course`` object. Task file paths in the manifest
        are relative to the manifest's directory (see ``create_course_manifest``).

        Parameters
        ----------
        fp: StrPath
            Path to the manifest
        check_progress: bool (optional, keyword only)
            Check the progress from the exercise files on disk (see ``Course``). Default
            is False.

        Returns
        -------
        Course
            The Course object represented in the manifest
        """
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)

        base = Path(fp).parent
        data["tasks"] = {k: paths.resolve(v, base) for k, v in data["tasks"].items()}

        return cls(**data, check_progress=check_progress)

    @staticmethod
    def create_course_manifest(
        fp: StrPath,
        task_files: Sequence[StrPath],
        *,
        name: Optional[str] = None,
        use_task_names: bool = True,
        progress: Optional[list[int]] = None,
    ) -> Path:
        """
        Compose a course manifest from the provided ``task_files``. A course manifest is
        a json file containing the course name, current progress, and an ordered mapping
        of task names and their file paths.

        Parameters
        ----------
        fp: StrPath
            Path to the course manifest. Should have the suffix '.pyicscourse.json'. If
            it doesn't, makes changes to the suffix and logs (if logging is active).
        task_files: Sequence[StrPath]
            List of task files to compose the course. Order is relevant. Given relative
            to the working directory (or absolute), and stored relative to the directory
            of the manifest, so the course keeps working when it is moved together with
            its tasks.
        name: str | None (optional, keyword only)
            Name of the course. If None, use ``fp``'s name without its suffix.
        use_task_names: bool (optional, keyword only)
            Use tasks' names for the mapping in the course manifest. Default is True.
            Setting this to False increases performance (no more json loading of tasks),
            but loses information (only enumerates tasks).
        progress: list[int] | None (optional, keyword only)
            Progress of each task, in the order of ``task_files``. Default is None, i.e.
            no task solved yet.

        Returns
        -------
        Path
            The path the manifest was written to, with its suffix normalized to
            '.pyicscourse.json'.
        """
        fp = Path(fp)

        match fp.suffixes:
            case []:
                fp = fp.with_suffix(".pyicscourse.json")
            case [_]:
                get_logger(__name__).warning(
                    f"Replacing suffix '{fp.suffix}' with '.pyicscourse.json'."
                )
                fp = fp.with_suffix(".pyicscourse.json")
            case [".pyicscourse", ".json"]:
                pass
            case _:
                get_logger(__name__).warning(
                    f"File suffixes don't match expected file path. Adding '.pyicscourse.json'."
                )
                fp = fp.with_suffix(fp.suffix + ".pyicscourse.json")

        manifest = {}

        manifest["name"] = name or fp.name.removesuffix(".pyicscourse.json")

        if use_task_names:

            def get_task_name(fp: StrPath) -> str:
                with open(fp, "r", encoding="utf-8") as f:
                    name = json.load(f)["name"]
                return name

            keys = map(get_task_name, task_files)

        else:
            width = len(str(len(task_files)))
            keys = [
                f"{manifest['name']}_{str(n).zfill(width)}" for n in range(len(task_files))
            ]

        manifest["tasks"] = {
            k: paths.relative_to(v, fp.parent) for k, v in zip(keys, task_files)
        }

        if progress is None:
            progress = [0] * len(task_files)
        elif len(progress) != len(task_files):
            raise ValueError(
                f"got progress for {len(progress)} tasks, but {len(task_files)} tasks"
            )
        manifest["progress"] = list(progress)

        # serialized before the file is opened, so a manifest that is being edited isn't
        # left empty if serializing fails
        text = json.dumps(manifest, indent=4)
        os.makedirs(fp.parent, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

        return fp
