import os
from os import PathLike
from pathlib import Path
from typing import Union

import jsonargparse

import pyics.json_handling as json
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

        # TODO: Do this properly. Probably use custon json serialization like I did in the trains dataset.
        # self.tests: list[Test] = [Test(test_dict) for test_dict in raw_json["tests"]]

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
        fp: PathLike,
        name: str,
        ex_files: Union[PathLike, list[PathLike], None] = None,
        sol_files: Union[PathLike, list[PathLike], None] = None,
        tests: Union[Test, list[Test], None] = None,
    ):
        """TODO
        Create a task_json from all relevant information as it would naturally be available
        when designing a task.

        Parameters
        ----------
        fp: PathLike
            Desired output path
        name: str
            Name of the task
        ex_files: Union[str, Path, list[str | Path]]
            Filename or list of filenames of exercise files.
        sol_files: Union[str, Path, list[str | Path]]
            Filename or list of filenames of solution files.
        tests: Union[Test, list[Test]]
            Filename or list of filenames containing serialized tests
        """
        fp = Path(fp)
        task: dict = {}

        task["name"] = name

        task["ex_files"] = (
            ex_files
            if isinstance(ex_files, list)
            else [ex_files]
            if ex_files is not None
            else []
        )
        task["sol_files"] = (
            sol_files
            if isinstance(sol_files, list)
            else [sol_files]
            if sol_files is not None
            else []
        )

        def read_file_to_string(fp: PathLike) -> str:
            with open(fp, "r") as f:
                return f.read()

        task["exercise_code"] = [read_file_to_string(f) for f in task["ex_files"]]
        task["solution_code"] = [read_file_to_string(f) for f in task["sol_files"]]
        task["tests"] = (
            tests if isinstance(tests, list) else [tests] if tests is not None else []
        )

        os.makedirs(fp.parent, exist_ok=True)
        with open(fp, "w") as f:
            json.dump(task, f)


def create_task():
    print(
        "The create_task script has not been set up yet. For now, use the create_task.ipynb notebook instead."
    )
    # jsonargparse.auto_cli(Task.create_task_json)


if __name__ == "__main__":
    create_task()
