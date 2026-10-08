import copy
import os
from collections.abc import Mapping, Sequence
from os import PathLike
from pathlib import Path
import threading
from typing import Any, Union

import pytest
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import pyics.util.json_handling as json
from pyics.distribution.dependencies import ModulePolicy, check_task_data
from pyics.util import paths, terminal
from pyics.util.paths import StrPath
from pyics.util.logging import get_logger
from pyics.tests import Test
from pyics.tests.pytest_integration import LiveTestCollectionPlugin, RichFailureRepr

# pytest runs aren't thread-safe (e.g. the tests import the exercise file as the module
# ``current_solution``), so all tasks' runs are serialized
_pytest_lock = threading.Lock()


def wait_for_test_runs(timeout: float = -1) -> None:
    """
    Wait until no task's tests are being run (at most ``timeout`` seconds, if given).
    A run restores the terminal mode it started in, so e.g. the terminal mode must not be
    changed back to cooked before a run started in raw mode has finished.
    """
    if _pytest_lock.acquire(timeout=timeout):
        _pytest_lock.release()


def _failure_text(report: pytest.TestReport) -> Text:
    """The failure message of ``report``, highlighted if it comes from a ``PyicsItem``"""
    if isinstance(report.longrepr, RichFailureRepr):
        return report.longrepr.text
    return Text(report.longreprtext)


def _failure_trace(report: pytest.TestReport) -> Text | None:
    """The trace of the failed test of ``report``, or why there is none, if it was run
    with tracing (see ``Task.tracing``)"""
    if isinstance(report.longrepr, RichFailureRepr):
        return report.longrepr.trace
    return None


def _traceable(test: Test) -> bool:
    """Whether ``test`` can be traced, and its author allows it (see ``Test.run``)"""
    return test.supports_trace and getattr(test, "traceable", True)


def _path_list(files: StrPath | Sequence[StrPath] | None) -> list[StrPath]:
    """``files`` as a list of paths, whether given as one path, several or None."""
    if files is None:
        return []
    if isinstance(files, (str, PathLike)):
        return [files]
    return list(files)


def _load_task_json(fp: StrPath, module_policy: ModulePolicy | None = None) -> dict:
    """
    Read and decode the task file at ``fp``, once it has been checked that decoding it
    imports only modules ``module_policy`` allows (if given), which are installed (see
    ``check_task_data``).
    """
    with open(fp, "r", encoding="utf-8") as f:
        text = f.read()
    check_task_data(json.loads_plain(text), fp, module_policy)
    return json.loads(text)


def _hint_list(hints: str | Sequence[str] | None) -> list[str]:
    """``hints`` as a list of hints, whether given as one hint, several or None."""
    if hints is None:
        return []
    if isinstance(hints, str):
        return [hints]
    return list(hints)


class Task:
    """Unified Task Representation"""

    __status: RenderableType
    __info: RenderableType
    __solved: bool = False
    __new_test_results: bool = False
    __tracing: bool = False
    # whether the current test results were traced
    __traced: bool = False

    def __init__(
        self,
        task_file: StrPath,
        *,
        setup: bool = True,
        module_policy: ModulePolicy | None = None,
    ):
        """
        Parses a task from a task json-file.

        File paths in the task file are relative to the task file's directory (see
        ``create_task_json``).

        Parameters
        ----------
        task_file: StrPath
            Path to the task file
        setup: bool (optional, keyword only)
            Create the task's files and run its tests (see ``setup``). Default is True.
            If False, the task can only be checked (see ``check_solved``) until ``setup``
            is called.
        module_policy: ModulePolicy | None (optional, keyword only)
            The modules the task file may name to decode the objects stored in it, i.e.
            those its course declares (see ``ModulePolicy``). If None, any module.

        Raises
        ------
        ModuleNotAllowedError
            If the task file names a module ``module_policy`` doesn't allow.
        MissingDependencyError
            If the task needs a package that isn't installed.
        """
        self.__path = task_file
        raw_json = _load_task_json(task_file, module_policy)

        base = Path(task_file).parent

        self.name: str = raw_json.get("name", "Task")
        self.instructions: str = raw_json.get("instructions", "")
        # revealed one at a time, in order (see ``Course.reveal_hint``)
        self.help: list[str] = _hint_list(raw_json.get("help"))

        self.exercise_files: list[Path] = [
            paths.resolve(fp, base) for fp in raw_json.get("ex_files", [])
        ]
        self.solution_files: list[Path] = [
            paths.resolve(fp, base) for fp in raw_json.get("sol_files", [])
        ]

        # kept until the files are created; the code does not need to be in memory after
        self.__code: tuple[list[str], list[str]] | None = (
            raw_json["exercise_code"],
            raw_json["solution_code"],
        )

        self.tests: list[Test] = raw_json["tests"]
        for test in self.tests:
            test.file_path = paths.resolve(test.file_path, base)

        self.__info = self.instructions
        self.__status = Text("tests pending...", style="dim")
        self.test_results: list[pytest.TestReport] = []
        self.__tested = False

        self._tests_lock = threading.Lock()
        self._tests_pending = False

        self.last_mtimes: dict[Path, float] = {}
        if setup:
            self.setup()

    def setup(self) -> None:
        """
        Create the task's files (keeping existing exercise files, see ``create_files``)
        and run its tests, unless they have already been run by ``check_solved``. Only
        does something the first time it is called.
        """
        if self.__code is None:
            return
        self.create_files(*self.__code)
        self.__code = None

        # files checked by check_solved keep the time they were checked at, so changes
        # made since are still picked up
        self.last_mtimes = {
            f: self.last_mtimes.get(f, os.path.getmtime(f))
            for f in self.exercise_files
            if os.path.exists(f)
        }
        if not self.__tested:
            self.run_tests()

    def check_solved(self) -> bool:
        """
        Run the tests synchronously and return whether the task is solved, without
        creating any files. A task whose exercise files don't all exist is unsolved, and
        its tests aren't run.
        """
        if not all(os.path.exists(f) for f in self.exercise_files):
            return False

        mtimes = {f: os.path.getmtime(f) for f in self.exercise_files}
        with self._tests_lock:
            self._tests_pending = True
            self.__run_tests_locked()
        # changes made from here on are picked up by check_file_changes
        self.last_mtimes = mtimes
        return self.solved

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
    def path(self) -> StrPath:
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
        if self.__traced:
            sections.append(
                Text("Tracing failed tests with snoop, press t to stop.", style="dim")
            )
        elif failed_reports and any(_traceable(t) for t in self.tests):
            sections.append(
                Text("Press t to trace failed tests with snoop.", style="dim")
            )

        if failed_reports:
            failures = [(r.nodeid, _failure_text(r)) for r in failed_reports]
            traces = [_failure_trace(r) for r in failed_reports]

            # one rule + traceback per failure, mirroring pytest's own failure sections
            sections.append(
                Padding(Rule("[white]FAILURES[/]", style="white"), pad=(1, 0, 0, 0))
            )
            for (nodeid, text), trace in zip(failures, traces):
                failure: list[RenderableType] = [
                    Padding(
                        Rule(Text(nodeid, style="bold bright_red"), style="bright_red"),
                        pad=(1, 0, 0, 0),
                    ),
                    Text.assemble(text, style="red"),
                ]
                if trace is not None:
                    failure += [
                        Padding(
                            Rule(Text("trace", style="dim"), style="dim"),
                            pad=(1, 0, 0, 0),
                        ),
                        trace,
                    ]
                sections.append(Group(*failure))

            # pytest's "short test summary info", one line per failed test
            sections.append(
                Padding(
                    Rule("[bright_cyan]short test summary info[/]", style="bright_cyan"),
                    pad=(1, 0, 0, 0),
                )
            )
            sections += [
                Text.assemble(
                    f"FAILED {nodeid} - ",
                    Text(" ").join(text.split()),
                    style="bright_red",
                )
                for nodeid, text in failures
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

    @property
    def tracing(self) -> bool:
        """
        Whether the tests are traced with ``snoop`` when they are run, to show the trace
        of each failed test below its failure (see ``Test.run``). Off for a new task.
        """
        return self.__tracing

    def toggle_tracing(self) -> None:
        """Turn ``tracing`` on or off, and run the tests again to show the change."""
        self.__tracing = not self.__tracing
        self.run_tests()

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
            self.__run_tests_locked()
        finally:
            self._tests_lock.release()

    def __run_tests_locked(self) -> None:
        """Run the tests while they are pending. ``self._tests_lock`` must be held."""
        while self._tests_pending:
            self._tests_pending = False
            tracing = self.__tracing
            plugin = LiveTestCollectionPlugin(self.tests, tracing=tracing)

            with _pytest_lock, terminal.preserve_terminal_mode():
                pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])

            self.test_results = plugin.reports
            self.__traced = tracing
            self.__tested = True

            # log reports
            logger = get_logger(__name__)
            for rep in self.test_results:
                logger.debug(f"{rep.nodeid} {rep.when} {rep.outcome}")
                logger.debug(rep.longreprtext)

            self.update_status()
            self.__new_test_results = True

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
    def read_task_json(fp: StrPath) -> dict[str, Any]:
        """
        Read a task file without loading the task, i.e. without creating its files or
        running its tests. Inverse of ``create_task_json``: returns its keyword
        arguments, so ``Task.create_task_json(fp, **Task.read_task_json(fp))`` rewrites
        the task as it is.

        File paths are resolved relative to the task file's directory, like in
        ``__init__``.

        Parameters
        ----------
        fp: StrPath
            Path to the task file

        Returns
        -------
        dict[str, Any]
            ``name``, ``instructions``, ``ex_files``, ``sol_files``, ``tests`` and
            ``help``, and the contents stored for the exercise and solution files as
            ``file_contents``, by path.
        """
        raw_json = _load_task_json(fp)

        base = Path(fp).parent

        ex_files = [paths.resolve(f, base) for f in raw_json.get("ex_files", [])]
        sol_files = [paths.resolve(f, base) for f in raw_json.get("sol_files", [])]

        file_contents = dict(zip(ex_files, raw_json.get("exercise_code", [])))
        file_contents.update(zip(sol_files, raw_json.get("solution_code", [])))

        tests: list[Test] = raw_json.get("tests") or []
        for test in tests:
            test.file_path = paths.resolve(test.file_path, base)

        return {
            "name": raw_json.get("name", "Task"),
            "instructions": raw_json.get("instructions", ""),
            "ex_files": ex_files,
            "sol_files": sol_files,
            "tests": tests,
            "file_contents": file_contents,
            "help": _hint_list(raw_json.get("help")),
        }

    @staticmethod
    def create_task_json(
        fp: StrPath,
        name: str,
        instructions: str,
        ex_files: StrPath | Sequence[StrPath] | None = None,
        sol_files: StrPath | Sequence[StrPath] | None = None,
        tests: Union[Test, list[Test], None] = None,
        file_contents: Mapping[StrPath, str] | None = None,
        help: str | Sequence[str] | None = None,
    ) -> Path:
        """
        Create a task_json from all relevant information as it would naturally be available
        when designing a task.

        File paths (``ex_files``, ``sol_files`` and the tests' ``file_path``) are given
        relative to the working directory (or absolute), and stored relative to the
        directory of the task file, so the task keeps working when it is moved together
        with its files.

        Parameters
        ----------
        fp: StrPath
            Desired output path
        name: str
            Name of the task
        instructions: str
            Instructions for the task
        ex_files: StrPath | Sequence[StrPath] | None
            Filename or list of filenames of exercise files.
        sol_files: StrPath | Sequence[StrPath] | None
            Filename or list of filenames of solution files.
        tests: Union[Test, list[Test]]
            Filename or list of filenames containing serialized tests
        file_contents: Mapping[StrPath, str] | None
            Contents to store for some of the exercise and solution files, by path,
            instead of reading these files, e.g. the contents already stored in the task
            file when editing it (see ``read_task_json``). Other files are read.
        help: str | Sequence[str] | None
            Hint or list of hints for the task, which the student can reveal one at a
            time, in order.

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
        task["help"] = _hint_list(help)

        ex_files = _path_list(ex_files)
        sol_files = _path_list(sol_files)
        task["ex_files"] = [paths.relative_to(f, base) for f in ex_files]
        task["sol_files"] = [paths.relative_to(f, base) for f in sol_files]

        contents = {paths.absolute(f): c for f, c in (file_contents or {}).items()}

        def read(f: StrPath) -> str:
            if (key := paths.absolute(f)) in contents:
                return contents[key]
            return Path(f).read_text(encoding="utf-8")

        task["exercise_code"] = [read(f) for f in ex_files]
        task["solution_code"] = [read(f) for f in sol_files]

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

        # serialized before the file is opened, so a task file that is being edited
        # isn't left empty if serializing fails
        text = json.dumps(task, indent=4)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

        return fp
