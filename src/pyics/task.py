from contextlib import redirect_stderr, redirect_stdout
import io
import os
from os import PathLike
from pathlib import Path
import threading
from typing import Union

import pytest
from rich.console import RenderableType
from rich.text import Text

import pyics.json_handling as json
from pyics.logging import get_logger
from pyics.tests import Test
from pyics.tests.pytest_integration import LiveTestCollectionPlugin


class Task:
    """Unified Task Representation"""

    __status: RenderableType
    __info: RenderableType
    __new_test_results: bool = False

    def __init__(self, task_file: str | Path):
        """
        Parses a task from a task json-file.

        Parameters
        ----------
        task_file: str | Path
            Path to the task file
        """
        self.__path = task_file
        with open(task_file, "r") as f:
            raw_json: dict = json.load(f)

        self.name: str = raw_json.get("name", "Task")
        self.instructions: str = raw_json.get("instructions", "")

        self.exercise_files: list[Path] = [Path(fp) for fp in raw_json.get("ex_files", [])]
        self.solution_files: list[Path] = [Path(fp) for fp in raw_json.get("sol_files", [])]

        # The solution and exercise code does not need to be in memory
        self.create_files(raw_json["exercise_code"], raw_json["solution_code"])

        self.tests: list[Test] = raw_json["tests"]

        self.last_mtimes = {
            f: os.path.getmtime(f) for f in self.exercise_files if os.path.exists(f)
        }

        self.__info = self.instructions
        self.__status = Text("tests pending...", style="dim")
        self.test_results: list[pytest.TestReport] = []

        self._tests_lock = threading.Lock()
        self._tests_pending = False

        self.run_tests()

    @property
    def new_test_results(self) -> bool:
        return self.__new_test_results

    def clear_test_results(self) -> None:
        """
        Set new_test_results to False, indicating that the new results have been
        processed.
        """
        self.__new_test_results = False

    @property
    def path(self) -> str | Path:
        return self.__path

    @property
    def info(self) -> RenderableType:
        """The task info, i.e. what is supposed to be displayed in the pyics header"""
        return self.__info

    @info.setter
    def info(self, v: str) -> None:
        """The task info, i.e. what is supposed to be displayed in the pyics header"""
        self.__info = v

    @property
    def status(self) -> RenderableType:
        """The task status, i.e. what is supposed to be displayed in the pyics main panel"""
        return self.__status

    def update_status(self) -> None:
        """Update the status from the test reports"""
        self.__status = Text(
            "\n\n".join([
                r.longreprtext
                for r in self.test_results
                if r.when == "call" and r.outcome == "failed"
            ])
        )

    def check_file_changes(self) -> bool:
        """
        Check whether an exercise file has been changed by monitoring the last modified
        timestamp.

        Returns
        -------
        bool
            True if any exercise file has been changed
        """
        changed = map(self._check_file_changed, self.exercise_files)
        return any(changed)

    def _check_file_changed(self, filepath: Path) -> bool:
        """
        Check whether a single exercise file has been changed by monitoring the last
        modified timestamp.

        Returns
        -------
        bool
            True if the file has been changed, False otherwise
        """
        if not os.path.exists(filepath):
            return False

        current_mtime = os.path.getmtime(filepath)
        if current_mtime > self.last_mtimes.get(filepath, 0):
            self.last_mtimes[filepath] = current_mtime
            return True

        return False

    def run_tests(self) -> None:
        """
        Runs all tests in self.tests using pytest and stores the unfiltered reports in
        self.test_results.

        Starts self.__run_tests as a daemon thread.
        """
        self._tests_pending = True
        if not self._tests_lock.acquire(blocking=False):
            return
        threading.Thread(target=self.__run_tests, daemon=True).start()

    def __run_tests(self) -> None:
        """
        DO NOT CALL THIS METHOD DIRECTLY. CALL run_tests INSTEAD.

        Runs all tests in self.tests synchronously using pytest and stores the unfiltered
        reports in self.test_results.
        """
        try:
            while self._tests_pending:
                self._tests_pending = False
                plugin = LiveTestCollectionPlugin(self.tests)

                pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])

                self.test_results = plugin.reports

                # log reports
                logger = get_logger(__name__)
                for rep in self.test_results:
                    logger.debug(f"{rep.nodeid} {rep.when} {rep.outcome}")
                    logger.debug(rep.longreprtext)

                self.update_status()
                self.__new_test_results = True
        finally:
            self._tests_lock.release()

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
        instructions: str,
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
        instructions: str
            Instructions for the task
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
        task["instructions"] = instructions

        task["ex_files"] = (
            ex_files
            if isinstance(ex_files, list)
            else [ex_files]
            if ex_files is not None
            else []
        )
        task["ex_files"] = [Path(f).absolute() for f in task["ex_files"]]
        task["sol_files"] = (
            sol_files
            if isinstance(sol_files, list)
            else [sol_files]
            if sol_files is not None
            else []
        )
        task["sol_files"] = [Path(f).absolute() for f in task["sol_files"]]

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
            json.dump(task, f, indent=4)


def create_task():
    print(
        "The create_task script has not been set up yet. For now, use the create_task.ipynb notebook instead."
    )
    # jsonargparse.auto_cli(Task.create_task_json)


if __name__ == "__main__":
    create_task()
