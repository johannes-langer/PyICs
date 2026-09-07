from __future__ import annotations

import collections
from fractions import Fraction
from os import PathLike
import os
from pathlib import Path
from typing import Optional

from pyics.data.task import Task
from pyics.util import json_handling as json
from pyics.util.logging import get_logger


class Course:
    """
    Course class. Handles creation and storage of courses, as well as safe task loading
    and keeping track of course progress.
    """

    name: str
    tasks: dict[str, PathLike]
    progress: Fraction

    def __init__(
        self, *, name: str, tasks: dict[str, PathLike], progress: Fraction
    ) -> None:
        self.name = name
        self.tasks = tasks
        self.__tasks_list = list(self.tasks)

        self.progress = progress

        # when you boot up a course, always show the next unsolved task
        self.__current_task: Task = Task(
            self.tasks[self.__tasks_list[int(self.progress * len(self.__tasks_list))]]
        )

    @property
    def current_task(self) -> Task:
        return self.__current_task

    def next(self) -> Task:
        """
        Select the next task. If the current task is unsolved, returns the current task.

        Returns
        -------
        Task
            Next task if the current task is solved, otherwise current task.
        """
        if self.current_task.solved:
            # TODO
            ...

        return self.current_task

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
        name: str = {v: k for k, v in self.tasks.items()}.get(self.current_task.path)  # pyright: ignore[reportAssignmentType]

        if (idx := self.__tasks_list.index(name)) != 0:
            self.__current_task = Task(self.tasks[self.__tasks_list[idx - 1]])

        return self.current_task

    def tick(self) -> tuple[bool, bool]:
        """
        Perform regular actions on the course, or pass them on to the current task. Does
        the following:

        1. Calls check_file_changes on the current task
        2. Calls run_tests on the current task if files have changed.
        3. checks if the course progress needs to be updated
        """
        files_changed = self.current_task.check_file_changes()

        if files_changed:
            self.current_task.run_tests()

        if self.current_task.solved:  # TODO: works only one way
            current_task_number = self.__tasks_list.index(
                {v: k for k, v in self.tasks.items()}.get(self.current_task.path)  # pyright: ignore[reportArgumentType]
            )
            required_progress = Fraction(current_task_number, len(self.__tasks_list))
            if required_progress > self.progress:
                self.progress = required_progress
                new_progress = True
            else:
                new_progress = False
        else:
            new_progress = False

        return (self.current_task.new_test_results, new_progress)

    def update_progress(self) -> bool:
        """
        Check if a progress update is required: Either because a previously unsolved
        task has been solved, or because a previously solved task has been broken.

        Returns
        -------
        bool
            True if self.progress has changed
        """
        # TODO
        ...

    @classmethod
    def load_course_manifest(cls, fp: PathLike) -> Course:
        """
        Load a course manifest to a ``Course`` object.

        Parameters
        ----------
        fp: PathLike
            Path to the manifest

        Returns
        -------
        Course
            The Course object represented in the manifest
        """
        with open(fp, "r") as f:
            data = json.load(f)

        ...
        return cls(**data)

    @staticmethod
    def create_course_manifest(
        fp: PathLike,
        task_files: list[PathLike],
        *,
        name: Optional[str] = None,
        use_task_names: bool = True,
    ) -> None:
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
            List of task files to compose the course. Order is relevant.
        name: str | None (optional, keyword only)
            Name of the course. If None, use ``fp``'s stem.
        use_task_names: bool (optional, keyword only)
            Use tasks' names for the mapping in the course manifest. Default is True.
            Setting this to False increases performance (no more json loading of tasks),
            but loses information (only enumerates tasks).
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
                with open(fp, "r") as f:
                    name = json.load(f)["name"]
                return name

            keys = map(get_task_name, task_files)

        else:
            width = len(str(len(task_files)))
            keys = [
                f"{manifest['name']}_{str(n).zfill(width)}" for n in range(len(task_files))
            ]

        manifest["tasks"] = {k: v for k, v in zip(keys, task_files)}

        manifest["progress"] = Fraction(0, len(task_files))

        os.makedirs(fp.parent, exist_ok=True)
        with open(fp, "w") as f:
            json.dump(manifest, f, indent=4)
