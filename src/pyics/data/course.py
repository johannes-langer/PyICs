from __future__ import annotations

from fractions import Fraction
from os import PathLike
import os
from pathlib import Path
from typing import Optional

from rich.console import RenderableType

from pyics.data.task import Task
from pyics.util import json_handling as json
from pyics.util import paths
from pyics.util.logging import get_logger


class Course:
    """
    Course class. Handles creation and storage of courses, as well as safe task loading
    and keeping track of course progress.
    """

    name: str
    tasks: dict[str, PathLike]

    def __init__(
        self, *, name: str, tasks: dict[str, PathLike], progress: list[int]
    ) -> None:
        self.name = name
        self.tasks = tasks
        self.__tasks_list = list(self.tasks)
        self.__progress: list[int] = progress

        # when you boot up a course, always show the next unsolved task
        self.__current_task: Task = Task(
            self.tasks[self.__tasks_list[int(self.__progress.index(0))]]
        )
        self.__task_changed: bool = True

    @property
    def progress(self) -> Fraction:
        return Fraction(sum(self.__progress), len(self.__progress))

    @property
    def instructions(self) -> str:
        return self.__current_task.instructions

    @property
    def info(self) -> RenderableType:
        """
        The current task's info, i.e. what is supposed to be displayed in the pyics header.
        """
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
        main panel.
        """
        return self.__current_task.status

    def update_status(self) -> None:
        """
        Expose the current task's update_status() method.
        """
        self.__current_task.update_status()

    @property
    def exercise_files(self) -> list[Path]:
        """
        The current task's exercise file paths.
        """
        return self.__current_task.exercise_files

    def next(self) -> Task:
        """
        Select the next task. If the current task is unsolved, returns the current task.

        Returns
        -------
        Task
            Next task if the current task is solved, otherwise current task.
        """
        if self.__current_task.solved:
            current_task_number = self.__tasks_list.index(
                {v: k for k, v in self.tasks.items()}.get(self.__current_task.path)  # pyright: ignore[reportArgumentType]
            )

            if current_task_number + 1 < len(self.__tasks_list):
                self.__current_task = Task(
                    self.tasks[self.__tasks_list[current_task_number + 1]]
                )
                self.__task_changed = True

        return self.__current_task

    def previous(self) -> Task:
        """
        Select the previous task (if available)

        Returns
        -------
        Task
            Previous task if the current task isn't the first task, otherwise current
            task.
        """
        # get course-specific name of current task
        current_task_number = self.__tasks_list.index(
            {v: k for k, v in self.tasks.items()}.get(self.__current_task.path)  # pyright: ignore[reportArgumentType]
        )

        if current_task_number != 0:
            self.__current_task = Task(
                self.tasks[self.__tasks_list[current_task_number - 1]]
            )
            self.__task_changed = True

        return self.__current_task

    @property
    def task_changed(self) -> bool:
        """
        Flag to indicate whether the current task has changed, set by next or previous.
        """
        return self.__task_changed

    def clear_task_changed(self) -> None:
        """Expose the option to reset task_changed"""
        self.__task_changed = False

    def clear_test_results(self) -> None:
        """Expose the current task's clear_test_results method"""
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
            whether a task has been newly solved/unsolved)
        """
        files_changed = self.__current_task.check_file_changes()

        if files_changed:
            self.__current_task.run_tests()

        current_task_number = self.__tasks_list.index(
            {v: k for k, v in self.tasks.items()}.get(self.__current_task.path)  # pyright: ignore[reportArgumentType]
        )
        if self.__current_task.solved:
            new_progress = not self.__progress[current_task_number] == 1
            self.__progress[current_task_number] = 1
        else:
            new_progress = self.__progress[current_task_number] == 1
            self.__progress[current_task_number] = 0

        return (self.__current_task.new_test_results, new_progress)

    @classmethod
    def load_course_manifest(cls, fp: PathLike) -> Course:
        """
        Load a course manifest to a ``Course`` object. Task file paths in the manifest
        are relative to the manifest's directory (see ``create_course_manifest``).

        Parameters
        ----------
        fp: PathLike
            Path to the manifest

        Returns
        -------
        Course
            The Course object represented in the manifest
        """
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)

        base = Path(fp).parent
        data["tasks"] = {k: paths.resolve(v, base) for k, v in data["tasks"].items()}

        return cls(**data)

    @staticmethod
    def create_course_manifest(
        fp: PathLike,
        task_files: list[PathLike],
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
        fp: PathLike
            Path to the course manifest. Should have the suffix '.pyicscourse.json'. If
            it doesn't, makes changes to the suffix and logs (if logging is active).
        task_files: list[PathLike]
            List of task files to compose the course. Order is relevant. Given relative
            to the working directory (or absolute), and stored relative to the directory
            of the manifest, so the course keeps working when it is moved together with
            its tasks.
        name: str | None (optional, keyword only)
            Name of the course. If None, use ``fp``'s stem.
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

        manifest["name"] = name or fp.stem

        if use_task_names:

            def get_task_name(fp: PathLike) -> str:
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
