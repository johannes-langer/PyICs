import copy
import os
from collections.abc import Mapping, Sequence
from os import PathLike
from pathlib import Path
import threading
import time
from typing import Any, Union

import pytest
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import pyics.util.json_handling as json
from pyics.distribution.dependencies import ModulePolicy, check_task_data
from pyics.notebook import cells
from pyics.notebook import sections as notebook_sections
from pyics.notebook.channel import (
    ChannelReader,
    ChannelState,
    ChannelStatus,
    LiveResults,
)
from pyics.util import paths, terminal
from pyics.util.paths import StrPath
from pyics.util.logging import get_logger
from pyics.tests import Test
from pyics.tests.pytest_integration import LiveTestCollectionPlugin
from pyics.tests.results import TestResult

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


def _failure_text(result: TestResult) -> Text:
    """The failure message of ``result``, highlighted if it comes from a ``PyicsItem``"""
    return result.message_text


def _failure_trace(result: TestResult) -> Text | None:
    """The trace of the failed test of ``result``, or why there is none, if it was run
    with tracing (see ``Task.tracing``)"""
    return result.trace_text


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


def _by_test(results: Sequence[TestResult]) -> dict[int, list[TestResult]]:
    """The phases of each test in ``results``, by the test's index (results without
    one are left out)."""
    by_test: dict[int, list[TestResult]] = {}
    for result in results:
        if result.index is not None:
            by_test.setdefault(result.index, []).append(result)
    return by_test


def _test_name(nodeid: str) -> str:
    """The name of the test whose pytest id is ``nodeid``, e.g. "adds" for "::adds"."""
    return nodeid.rpartition("::")[2] or nodeid


def _shown_live(live: list[TestResult], saved: list[TestResult] | None) -> bool:
    """
    Whether the ``live`` result of a test (its phases) is shown rather than its
    ``saved`` one, given that it is newer: unless it was skipped (e.g. a ``PrintTest``,
    which only runs on the saved files), or it is another test's (the kernel and the
    TUI loaded different versions of the task file).
    """
    if saved is None:
        return True
    return live[0].nodeid == saved[0].nodeid and all(
        r.outcome != "skipped" for r in live
    )


def _describe_run(run: LiveResults) -> str:
    """The cell and the time of a kernel's live ``run``, e.g. "cell 5, 12:03:41"."""
    at = time.strftime("%H:%M:%S", time.localtime(run.time))
    if run.cell is None:
        return at
    if run.cell.number is None:  # it isn't in the saved notebook
        return f"an unsaved cell, {at}"
    return f"cell {run.cell.number}, {at}"


# the order in which the channels of a task's notebooks are told about, if no kernel
# of them has results for the task
_STATUS_ORDER = [
    ChannelStatus.CONNECTED,
    ChannelStatus.RESTARTED,
    ChannelStatus.TIMED_OUT,
    ChannelStatus.INCOMPATIBLE,
    ChannelStatus.NO_KERNEL,
]

SOLVED_LIVE = "Solved in your notebook — save it to count it."


def _hint_list(hints: str | Sequence[str] | None) -> list[str]:
    """``hints`` as a list of hints, whether given as one hint, several or None."""
    if hints is None:
        return []
    if isinstance(hints, str):
        return [hints]
    return list(hints)


def _read_sections(raw_json: Mapping[str, Any], base: Path) -> dict[Path, str]:
    """The ``sections`` of a task file read from the directory ``base``, by notebook
    path resolved relative to ``base`` (see ``create_task_json``). Entries that aren't a
    path and the name of a section are left out."""
    stored = raw_json.get("sections")
    if not isinstance(stored, Mapping):
        return {}
    return {
        paths.resolve(path, base): name
        for path, name in stored.items()
        if isinstance(path, str) and notebook_sections.is_name(name)
    }


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
        # the section of each exercise notebook the task is about, by the notebook's
        # absolute path (see ``pyics.notebook.sections``). Its tests inherit it, unless
        # they have a section of their own (see ``Test.resolved_section``).
        self.sections: dict[Path, str] = {
            paths.absolute(path): name
            for path, name in _read_sections(raw_json, base).items()
        }

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
        self.test_results: list[TestResult] = []
        self.__tested = False

        self._tests_lock = threading.Lock()
        self._tests_pending = False
        # when the version of the exercise files that ``test_results`` come from was
        # saved (see ``shown_results``)
        self.test_results_time: float | None = None

        # live results: the exercise notebooks' channels, opened by the first
        # ``check_live_results``, what they said last, the results shown, and where
        # they come from
        self.__notebooks = list(
            dict.fromkeys(
                paths.absolute(f) for f in self.exercise_files if cells.is_notebook(f)
            )
        )
        self.__channels: dict[Path, ChannelReader] | None = None
        self.__channel_states: dict[Path, ChannelState] = {}
        self.live_results: list[TestResult] = []
        self.live_state: ChannelState | None = None
        # the status is updated by test runs and by live results, in different threads
        self.__status_lock = threading.RLock()

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
        with self.__status_lock:
            self.__update_status()

    def __update_status(self) -> None:
        results, live = self.shown_results()
        # filter tests for only call tests
        relevant_reports = [r for r in results if r.when == "call"]
        passed = [r.outcome == "passed" for r in relevant_reports]
        failed_reports = [r for r in relevant_reports if r.outcome == "failed"]
        pass_rate = round(100 * sum(passed) / len(passed)) if passed else 0

        # progress only counts the results of the saved files, never live ones
        saved = [r.outcome == "passed" for r in self.test_results if r.when == "call"]
        self.__solved = bool(saved) and round(100 * sum(saved) / len(saved)) == 100

        # result summary, box=None so the row reaches the panel's own border
        summary = Table(expand=True, show_header=False, box=None, padding=0)
        summary.add_column()
        summary.add_column(justify="right")
        summary.add_row(
            # live results are underlined
            "".join(
                f"[{'bright_green' if ok else 'bright_red'}"
                f"{' underline' if r.index in live else ''}]{'.' if ok else 'F'}[/]"
                for r, ok in zip(relevant_reports, passed)
            ),
            f"[{'bright_green' if pass_rate == 100 else 'bright_red'}]{pass_rate}%[/]",
        )

        sections: list[RenderableType] = [summary]
        if self.__notebooks:
            sections += self.__live_status(relevant_reports, live)
        if self.__traced:
            sections.append(
                Text("Tracing failed tests with snoop, press t to stop.", style="dim")
            )
        elif failed_reports and any(_traceable(t) for t in self.tests):
            sections.append(
                Text("Press t to trace failed tests with snoop.", style="dim")
            )

        if failed_reports:
            # live failures are labelled as such
            failures = [
                (f"{r.nodeid} (live)" if r.index in live else r.nodeid, _failure_text(r))
                for r in failed_reports
            ]
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

    def shown_results(self) -> tuple[list[TestResult], set[int]]:
        """
        The results the status shows: those of the saved files (``test_results``),
        where each test's result is replaced by the live one of a notebook's kernel
        (``live_results``) if that is newer, i.e. if the kernel ran it after the
        version of the files that ``test_results`` come from was saved. Live results
        that were skipped (``PrintTest``s, which only run on the saved files) don't
        replace saved ones, and tests without a live result (e.g. after an interrupted
        live run) keep theirs.

        Returns
        -------
        tuple[list[TestResult], set[int]]
            The results, and the indices of the tests whose results are live.
        """
        with self.__status_lock:
            live_run = None if self.live_state is None else self.live_state.results
            saved_time = self.test_results_time
            if not self.live_results or live_run is None:
                return self.test_results, set()
            if saved_time is not None and saved_time >= live_run.time:
                return self.test_results, set()

            live = _by_test(self.live_results)
            saved = _by_test(self.test_results)
            shown = {i for i, r in live.items() if _shown_live(r, saved.get(i))}
            results: list[TestResult] = []
            for result in self.test_results:
                if result.index not in shown:
                    results.append(result)
                elif result is saved[result.index][0]:
                    results += live[result.index]
            # tests without a saved result, e.g. while the files are first tested
            for i in sorted(shown - saved.keys()):
                results += live[i]
            return results, shown

    def __live_status(
        self, shown: list[TestResult], live: set[int]
    ) -> list[RenderableType]:
        """
        The lines below the result summary of a task with notebooks: where the
        ``shown`` results (those of the tests' calls) come from, the tests that pass in
        the notebook's kernel but not in the saved notebook, and whether the task is
        solved in the kernel, but not yet in the saved notebook. Results of the tests
        whose indices are in ``live`` are live.
        """
        state = self.live_state
        if state is None:  # the channels haven't been looked at yet
            return []
        live_run = state.results
        lines: list[RenderableType] = []
        if live:
            assert live_run is not None
            source = (
                f"Live results from your notebook kernel ({_describe_run(live_run)}). "
                "Save the notebook to count your progress."
            )
        elif state.status is ChannelStatus.TIMED_OUT:
            source = (
                "No notebook kernel connected: your notebook kernel stopped "
                "responding, so results are from your saved notebook."
            )
        elif state.status is not ChannelStatus.CONNECTED:
            source = state.reason or ""
        elif live_run is None:
            source = (
                "Your notebook kernel is connected: run a cell to see live results."
            )
        elif not self.live_results and live_run.for_task(self.path) is None:
            source = (
                "Your notebook kernel doesn't run this task's tests: results are "
                "from your saved notebook."
            )
        elif self.test_results_time is not None and (
            self.test_results_time >= live_run.time
        ):
            source = (
                "Results from your saved notebook, which was saved after your last "
                f"cell run ({_describe_run(live_run)})."
            )
        else:
            source = "Results from your saved notebook."
        lines.append(Text(source, style="dim"))

        # the usual sign of state the kernel holds, but the saved notebook doesn't
        live_calls = {r.index: r for r in self.live_results if r.when == "call"}
        disagreeing = [
            _test_name(r.nodeid)
            for r in self.test_results
            if r.when == "call"
            and r.outcome == "failed"
            and r.index is not None
            and (other := live_calls.get(r.index)) is not None
            and other.outcome == "passed"
            and other.nodeid == r.nodeid
        ]
        if disagreeing:
            names = ", ".join(disagreeing[:-1])
            names = f"{names} and {disagreeing[-1]}" if names else disagreeing[0]
            lines.append(
                Text(
                    f"{names} {'pass' if len(disagreeing) > 1 else 'passes'} in your "
                    "kernel, but not in the saved notebook: was a cell deleted or not "
                    "saved?",
                    style="yellow",
                )
            )

        if self.__solved_live(shown, live):
            lines.append(Text(SOLVED_LIVE, style="bright_green"))
        return lines

    def __solved_live(self, shown: list[TestResult], live: set[int]) -> bool:
        """Whether the ``shown`` results of the tests' calls all pass, some of them
        live (those of the tests whose indices are in ``live``), while the task isn't
        ``solved`` in the saved files."""
        return (
            bool(live)
            and not self.__solved
            and bool(shown)
            and all(r.outcome == "passed" for r in shown)
        )

    @property
    def solved(self) -> bool:
        """Whether all tests pass on the saved exercise files. Live results don't
        count, as they may come from code that isn't saved (see ``shown_results``)."""
        return self.__solved

    @property
    def solved_live(self) -> bool:
        """Whether all tests pass as shown, with live results (see ``shown_results``),
        although the task isn't ``solved`` in the saved files yet."""
        with self.__status_lock:
            results, live = self.shown_results()
            return self.__solved_live([r for r in results if r.when == "call"], live)

    @property
    def tracing(self) -> bool:
        """
        Whether the tests are traced with ``snoop`` when they are run, to show the trace
        of each failed test below its failure (see ``Test.run``). Off for a new task.
        """
        return self.__tracing

    def toggle_tracing(self) -> None:
        """Turn ``tracing`` on or off, and run the tests again to show the change. The
        kernels of the task's notebooks trace their next live runs, too (or stop)."""
        self.__tracing = not self.__tracing
        for notebook, reader in self.__open_channels().items():
            try:
                reader.write_control(tracing=self.__tracing)
            except Exception:  # noqa: BLE001
                get_logger(__name__).warning(
                    f"Can't ask the kernels of {notebook} to trace", exc_info=True
                )
        self.run_tests()

    def __open_channels(self) -> dict[Path, ChannelReader]:
        """The channels of the task's notebooks, opened the first time. The kernels
        don't trace when a task is opened, as its own tests aren't traced either."""
        if self.__channels is None:
            self.__channels = {}
            for notebook in self.__notebooks:
                reader = ChannelReader(notebook)
                self.__channels[notebook] = reader
                try:
                    # only written if needed, so that no channel is created for it
                    if reader.read_control().tracing:
                        reader.write_control(tracing=False)
                except Exception:  # noqa: BLE001
                    get_logger(__name__).warning(
                        f"Can't ask the kernels of {notebook} not to trace",
                        exc_info=True,
                    )
        return self.__channels

    def check_live_results(self) -> bool:
        """
        Look for new live results in the channels of the task's exercise notebooks,
        which their kernels write when cells run (see ``pyics.notebook.channel``), and
        show them (see ``shown_results``), or why there are none. Cheap enough to call
        at every tick: the channel's files are only read when they changed. Does
        nothing for a task without notebooks.

        Returns
        -------
        bool
            True if the live results, or where they come from, changed
        """
        if not self.__notebooks:
            return False
        changed = False
        for notebook, reader in self.__open_channels().items():
            try:
                state = reader.poll()
            except Exception:  # noqa: BLE001
                # read again at the next tick
                get_logger(__name__).debug(
                    f"Can't read the channel of {notebook}", exc_info=True
                )
                continue
            if state is not self.__channel_states.get(notebook):
                self.__channel_states[notebook] = state
                changed = True
        if not changed:
            return False

        with self.__status_lock:
            self.live_state, self.live_results = self.__connected()
            self.update_status()
            self.__new_test_results = True
        return True

    def __connected(self) -> tuple[ChannelState | None, list[TestResult]]:
        """The channel state to show, and its live results of this task: of the
        notebooks' connected kernels with results of the task, the one whose results
        are newest, or else the channel that tells most about why there are none."""
        newest: tuple[float, ChannelState, list[TestResult]] | None = None
        for state in self.__channel_states.values():
            run = state.results
            if run is None or (results := run.for_task(self.path)) is None:
                continue
            if newest is None or run.time > newest[0]:
                newest = (run.time, state, results)
        if newest is not None:
            return newest[1], newest[2]
        states = sorted(
            self.__channel_states.values(),
            key=lambda state: _STATUS_ORDER.index(state.status),
        )
        return (states[0] if states else None), []

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
            plugin = LiveTestCollectionPlugin(
                self.tests, tracing=tracing, sections=self.sections
            )
            saved_at = self.__saved_at()

            with _pytest_lock, terminal.preserve_terminal_mode():
                pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])

            # log reports
            logger = get_logger(__name__)
            for result in plugin.results:
                logger.debug(f"{result.nodeid} {result.when} {result.outcome}")
                logger.debug(_failure_text(result).plain)

            with self.__status_lock:
                self.test_results = plugin.results
                self.test_results_time = saved_at
                self.__traced = tracing
                self.__tested = True
                self.update_status()
                self.__new_test_results = True

    def __saved_at(self) -> float:
        """When the exercise files were last saved: the latest of their modification
        times, or now if none can be told. Live results newer than that are shown
        rather than the results of the tests run on the files (see
        ``shown_results``)."""
        times = []
        for f in self.exercise_files:
            try:
                times.append(os.path.getmtime(f))
            except OSError:
                pass
        return max(times, default=time.time())

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
            ``name``, ``instructions``, ``ex_files``, ``sol_files``, ``tests``,
            ``help`` and ``sections`` (by notebook path, resolved like ``ex_files``),
            and the contents stored for the exercise and solution files as
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
            "sections": _read_sections(raw_json, base),
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
        sections: Mapping[StrPath, str] | None = None,
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
            file when editing it (see ``read_task_json``). Other files are read. The
            contents of notebooks are stored normalized (see ``cells.normalize``), e.g.
            without their outputs.
        help: str | Sequence[str] | None
            Hint or list of hints for the task, which the student can reveal one at a
            time, in order.
        sections: Mapping[StrPath, str] | None
            The section the task is about (see ``pyics.notebook.sections``) of some of
            its exercise notebooks, by path. Its tests inherit it, unless they have a
            section of their own. Stored as ``sections``, by the notebook's path
            relative to the task file (with forward slashes), and left out if empty.
            Older versions of pyics ignore it.

        Returns
        -------
        Path
            The path the task file was written to, with its suffix normalized to
            '.pyicstask.json'.

        Raises
        ------
        ValueError
            If a path of ``sections`` isn't one of the exercise notebooks, or a name in
            it can't be the name of a section.
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

        notebooks = {paths.absolute(f) for f in ex_files if cells.is_notebook(f)}
        stored_sections = {}
        for path, section in (sections or {}).items():
            if paths.absolute(path) not in notebooks:
                raise ValueError(
                    f"{path} has a section, but isn't one of the exercise notebooks"
                )
            key = paths.relative_to(path, base).as_posix()
            stored_sections[key] = notebook_sections.check_name(section)
        if stored_sections:
            task["sections"] = stored_sections

        contents = {paths.absolute(f): c for f, c in (file_contents or {}).items()}

        def read(f: StrPath) -> str:
            if (key := paths.absolute(f)) in contents:
                text = contents[key]
            else:
                text = Path(f).read_text(encoding="utf-8")
            if cells.is_notebook(f):
                # without outputs, which would leak the solution's, and bloat the file
                return cells.normalize(text, name=Path(f).name)
            return text

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
