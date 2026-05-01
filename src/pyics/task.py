import json
import os
from pathlib import Path
from typing import Union

from snoop import snoop

from pyics.tests import Test


class Task:
    """Unified Task Representation"""

    def __init__(self, task_file: str | Path):
        """
        Parses a task form a task json-file.

        Parameters
        ----------
        task_file: str | Path
            Path to the task file
        """
        with open(task_file, "r") as f:
            raw_json: dict = json.load(f)

        self.name = raw_json["name"]
        self.exercise_files: list[Path] = [Path(fp) for fp in raw_json["ex_files"]]
        self.solution_files: list[Path] = [Path(fp) for fp in raw_json["sol_files"]]

        self.create_files(raw_json["exercise_code"], raw_json["solution_code"])

        self.tests: list[Test] = [Test(test_dict) for test_dict in raw_json["tests"]]

        self.last_mtimes = {
            f: os.path.getmtime(f) for f in self.exercise_files if os.path.exists(f)
        }

    def check_file_changes(self) -> None:
        """
        Check whether an exercise file has been changed by monitoring the last modified
        timestamp.
        """
        for filepath in self.exercise_files:
            self._check_file_changed(filepath)

    def _check_file_changed(self, filepath: Path):
        """ """
        if not os.path.exists(filepath):
            pass

        current_mtime = os.path.getmtime(filepath)
        if current_mtime > self.last_mtimes.get(filepath, 0):
            self.last_mtimes[filepath] = current_mtime
            self.run_file_on_change(filepath)

    def run_file_on_change(self, filepath: Path) -> None:
        """ """

    def create_files(self, exercise_code: list[str], solution_code: list[str]) -> None:
        """
        Create the exercise and solution files. Assumes that filepaths in
        self.exercise_files and self.solution_files are in the same order as the code
        lists, respectively.

        Parameters
        ----------
        exercise_code: list[str]
            List of file-contents for the exercise files
        solution_code: list[str]
            List of file-contents for the solution files
        """

        for fp, code in zip(self.exercise_files, exercise_code):
            with open(fp, "w") as f:
                f.write(code)

        for fp, code in zip(self.solution_files, solution_code):
            with open(fp, "w") as f:
                f.write(code)

    @staticmethod
    def create_task_json(
        fp: Union[str, Path],
        name: str,
        ex_files: Union[str, Path, list[str | Path]],
        sol_files: Union[str, Path, list[str | Path]],
        tests: Union[Test, list[Test]],
    ):
        """TODO
        Create a task_json from all relevant information as it would naturally be available
        when designing a task.

        Parameters
        ----------
        fp: Union[str, Path]
            Desired output path
        name: str
            Name of the task
        ex_files: Union[str, Path, list[str | Path]]
            Filename or list of filenames of exercise files.
        sol_files: Union[str, Path, list[str | Path]]
            Filename or list of filenames of solution files.
        tests: Union[Test, list[Test]]
            Test or list of tests the exercise tasks must pass for the task to count as
            completed.
        """
        task: dict = {}

        ...

        with open(fp, "W") as f:
            json.dump(task, f)
