import copy
import os
from os import PathLike
from pathlib import Path
import sys
import termios
import threading
from typing import Union

import pytest
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import pyics.util.json_handling as json
from pyics.util import paths
from pyics.util.logging import get_logger
from pyics.tests import Test
from pyics.tests.pytest_integration import LiveTestCollectionPlugin


class Task:
    """Unified Task Representation"""

    __status: RenderableType
    __info: RenderableType
    __solved: bool = False
    __new_test_results: bool = False

    def __init__(self, task_file: PathLike):
        """
        Parses a task from a task json-file.

        File paths in the task file are relative to the task file's directory (see
        ``create_task_json``).

        Parameters
        ----------
        task_file: PathLike
            Path to the task file
        """
        self.__path = task_file
        with open(task_file, "r", encoding="utf-8") as f:
            raw_json: dict = json.load(f)

        base = Path(task_file).parent

        self.name: str = raw_json.get("name", "Task")
        self.instructions: str = raw_json.get("instructions", "")

        self.exercise_files: list[Path] = [
            paths.resolve(fp, base) for fp in raw_json.get("ex_files", [])
        ]
        self.solution_files: list[Path] = [
            paths.resolve(fp, base) for fp in raw_json.get("sol_files", [])
        ]

        # The solution and exercise code does not need to be in memory
        self.create_files(raw_json["exercise_code"], raw_json["solution_code"])

        self.tests: list[Test] = raw_json["tests"]
        for test in self.tests:
            test.file_path = paths.resolve(test.file_path, base)

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
    def path(self) -> PathLike:
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
        # filter tests for only call tests
        relevant_reports = [r for r in self.test_results if r.when == "call"]
        passed = [r.outcome == "passed" for r in relevant_reports]
        failed_reports = [r for r in relevant_reports if r.outcome == "failed"]
        pass_rate = round(100 * sum(passed) / len(passed)) if passed else 0

        self.__solved = pass_rate == 100

        # result summary, box=None so the row reaches the panel's own border
        summary = Table(expand=True, show_header=False, box=None, padding=0)
        summary.add_column()
        summary.add_column(justify="right")
        summary.add_row(
            "".join("[bright_green].[/]" if ok else "[bright_red]F[/]" for ok in passed),
            f"[{'bright_green' if pass_rate == 100 else 'bright_red'}]{pass_rate}%[/]",
        )

        sections: list[RenderableType] = [summary]

        if failed_reports:
            # one rule + traceback per failure, mirroring pytest's own failure sections
            sections.append(
                Padding(Rule("[white]FAILURES[/]", style="white"), pad=(1, 0, 0, 0))
            )
            sections += [
                Group(
                    Padding(
                        Rule(f"[bold bright_red]{r.nodeid}[/]", style="bright_red"),
                        pad=(1, 0, 0, 0),
                    ),
                    Text(r.longreprtext, style="red"),
                )
                for r in failed_reports
            ]

            # pytest's "short test summary info", one line per failed test
            sections.append(
                Padding(
                    Rule("[bright_cyan]short test summary info[/]", style="bright_cyan"),
                    pad=(1, 0, 0, 0),
                )
            )
            sections += [
                Text(
                    f"FAILED {r.nodeid} - {' '.join(r.longreprtext.splitlines())}",
                    style="bright_red",
                )
                for r in failed_reports
            ]

        if relevant_reports:
            n_failed, n_passed = len(failed_reports), sum(passed)
            duration = sum(r.duration for r in relevant_reports)

            counts = Text()
            if n_failed:
                counts.append(f"{n_failed} failed", style="bold bright_red")
            if n_failed and n_passed:
                counts.append(", ", style="white")
            if n_passed:
                counts.append(f"{n_passed} passed", style="bright_green")
            counts.append(
                f" in {duration:.2f}s", style="bright_red" if n_failed else "bright_green"
            )
            sections.append(Padding(counts, pad=(1, 0, 0, 0)))

        self.__status = Group(*sections)

    @property
    def solved(self) -> bool:
        return self.__solved

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

                try:
                    tty_attrs = termios.tcgetattr(sys.stdin.fileno())
                except termios.error, ValueError:
                    tty_attrs = None

                try:
                    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
                finally:
                    if tty_attrs is not None:
                        try:
                            termios.tcsetattr(
                                sys.stdin.fileno(), termios.TCSANOW, tty_attrs
                            )
                        except termios.error:
                            pass

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

    def create_files(
        self,
        exercise_code: list[str],
        solution_code: list[str],
        *,
        overwrite: bool = False,
    ) -> None:
        """
        Create the exercise and solution files. Assumes that filepaths in
        self.exercise_files and self.solution_files are in the same order as the code
        lists, respectively.

        Existing exercise files are kept (they hold the student's work) unless
        ``overwrite`` is set. Solution files are always rewritten. Missing parent
        directories are created.

        Parameters
        ----------
        exercise_code: list[str]
            List of file-contents for the exercise files
        solution_code: list[str]
            List of file-contents for the solution files
        overwrite: bool (optional, keyword only)
            Reset existing exercise files to the exercise code. Default is False.
        """

        for fp, code in zip(self.exercise_files, exercise_code):
            if not overwrite and fp.exists():
                continue
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(code, encoding="utf-8")

        for fp, code in zip(self.solution_files, solution_code):
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(code, encoding="utf-8")

    @staticmethod
    def create_task_json(
        fp: PathLike,
        name: str,
        instructions: str,
        ex_files: Union[PathLike, list[PathLike], None] = None,
        sol_files: Union[PathLike, list[PathLike], None] = None,
        tests: Union[Test, list[Test], None] = None,
    ) -> Path:
        """TODO
        Create a task_json from all relevant information as it would naturally be available
        when designing a task.

        File paths (``ex_files``, ``sol_files`` and the tests' ``file_path``) are given
        relative to the working directory (or absolute), and stored relative to the
        directory of the task file, so the task keeps working when it is moved together
        with its files.

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

        Returns
        -------
        Path
            The path the task file was written to, with its suffix normalized to
            '.pyicstask.json'.
        """
        fp = Path(fp)
        base = fp.parent
        task: dict = {}

        task["name"] = name
        task["instructions"] = instructions

        ex_files = (
            ex_files
            if isinstance(ex_files, list)
            else [ex_files]
            if ex_files is not None
            else []
        )
        sol_files = (
            sol_files
            if isinstance(sol_files, list)
            else [sol_files]
            if sol_files is not None
            else []
        )
        task["ex_files"] = [paths.relative_to(f, base) for f in ex_files]
        task["sol_files"] = [paths.relative_to(f, base) for f in sol_files]

        task["exercise_code"] = [Path(f).read_text(encoding="utf-8") for f in ex_files]
        task["solution_code"] = [Path(f).read_text(encoding="utf-8") for f in sol_files]

        tests = tests if isinstance(tests, list) else [tests] if tests is not None else []
        task["tests"] = []
        for test in tests:
            # copied, so the caller's test keeps its path
            test = copy.copy(test)
            test.file_path = paths.relative_to(test.file_path, base)
            task["tests"].append(test)

        os.makedirs(base, exist_ok=True)

        match fp.suffixes:
            case []:
                fp = fp.with_suffix(".pyicstask.json")
            case [_]:
                get_logger(__name__).warning(
                    f"Replacing suffix '{fp.suffix}' with '.pyicstask.json'."
                )
                fp = fp.with_suffix(".pyicstask.json")
            case [".pyicstask", ".json"]:
                pass
            case _:
                get_logger(__name__).warning(
                    f"File suffixes don't match expected file path. Adding '.pyicstask.json'."
                )
                fp = fp.with_suffix(fp.suffix + ".pyicstask.json")

        with open(fp, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=4)

        return fp
