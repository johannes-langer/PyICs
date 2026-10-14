import dataclasses
import threading
from collections.abc import Mapping, Sequence
from os import PathLike
from pathlib import Path
from typing import Literal

import pytest
from pytest import TestReport
from _pytest._code.code import TerminalRepr
from _pytest._io import TerminalWriter
from _pytest.nodes import Node
from rich.errors import MarkupError
from rich.style import Style
from rich.text import Text

from pyics.notebook import loader
from pyics.tests import Test, general
from pyics.tests.general import absolute_sections
from pyics.tests.results import TestResult
from pyics.tests.tests import TASK_SECTIONS, GeneralTest, MarkupAssertionError
from pyics.tests.trace import Trace, ansi_text
from pyics.util import terminal
from pyics.util.paths import StrPath

# pytest runs aren't thread-safe (e.g. the tests import the exercise file as the module
# ``current_solution``), so all runs are serialized (see ``run_tests``)
_pytest_lock = threading.Lock()

# the position of an item's test in the tests that are run (see ``TestResult.index``)
INDEX_KEY = pytest.StashKey[int | None]()


def trace_ansi(test: Test, *, tracing: bool, trace: Trace | None) -> str | None:
    """
    The trace of a failed ``test``, or why there is none (dimmed), if it ran with
    ``tracing``, as text with ANSI escape codes (see ``Trace.ansi``): ``trace``, the
    trace that was recorded, if the test could be traced.
    """
    if not tracing:
        return None
    dim = Style(dim=True).render
    if not test.supports_trace:
        return dim(f"{type(test).__name__}s can't be traced.")
    if not getattr(test, "traceable", True):
        return dim("Tracing is turned off for this test.")
    if trace is None or not (ansi := trace.ansi):
        return dim("Nothing was traced: the test failed before running your code.")
    return ansi


@dataclasses.dataclass(eq=False)
class RichFailureRepr(TerminalRepr):
    """
    Failure representation of a ``PyicsItem``, keeping the highlighting of its message
    (and its ``trace``, if the test was traced) for the main panel. Elsewhere, e.g. in
    ``TestReport.longreprtext``, it is plain text.
    """

    text: Text
    # the trace as text with ANSI escape codes (see ``Trace.ansi``), which a
    # ``TestResult`` keeps as it is
    trace_ansi: str | None = None

    @property
    def trace(self) -> Text | None:
        """The trace of the failed test, or why there is none, if it was traced."""
        return None if self.trace_ansi is None else ansi_text(self.trace_ansi)

    def toterminal(self, tw: TerminalWriter) -> None:
        tw.line(self.text.plain)
        if (trace := self.trace) is not None:
            tw.line(trace.plain)


class PyicsItem(pytest.Item):
    """
    pytest.Item intended to wrap the tests below for access with pytest

    """

    def __init__(
        self,
        *,
        name: str,
        parent: Node,
        test_obj: Test,
        tracing: bool = False,
        sections: Mapping[StrPath, str] | None = None,
    ):
        self.test_obj = test_obj
        # whether the test is traced, if it can be (see ``Test.run``), to show the trace
        # if it fails
        self.tracing = tracing
        # the sections of its task, which it inherits while it runs (see
        # ``TASK_SECTIONS``)
        self.sections: Mapping[Path, str] = absolute_sections(sections)
        self.recorded_trace: Trace | None = None
        # the errors in the cells of a notebook the test ran, named if it fails
        self.cell_errors: list[loader.CellError] = []
        super().__init__(name=self.test_obj.name, parent=parent)

    def runtest(self) -> None:
        loader.clear_last_errors()
        task_sections = TASK_SECTIONS.set(self.sections)
        try:
            if not (self.tracing and self.test_obj.supports_trace):
                self.test_obj.run()
                return
            # tests whose authors don't allow it ignore the trace
            self.recorded_trace = Trace()
            self.test_obj.run(trace=self.recorded_trace)
        finally:
            self.cell_errors = loader.last_errors()
            TASK_SECTIONS.reset(task_sections)

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: None
        | Literal["long"]
        | Literal["short"]
        | Literal["line"]
        | Literal["no"]
        | Literal["native"]
        | Literal["value"]
        | Literal["auto"] = None,
    ) -> str | TerminalRepr:
        message = Text(str(excinfo.value))
        if isinstance(excinfo.value, MarkupAssertionError):
            try:
                message = Text.from_markup(str(excinfo.value), emoji=False)
            except MarkupError:
                pass  # shown as it is, rather than failing the whole run
        for error in self.cell_errors:  # which may be why the test failed
            message = Text.assemble(message, "\n", error.describe())
        return RichFailureRepr(
            Text.assemble(f"{self.name} failed: ", message), self._trace_ansi()
        )

    def _trace_ansi(self) -> str | None:
        """The trace of the failed test, or why there is none (dimmed), if it is
        traced (see ``trace_ansi``)."""
        return trace_ansi(
            self.test_obj, tracing=self.tracing, trace=self.recorded_trace
        )

    def reportinfo(self) -> tuple[PathLike | str, int | None, str]:
        return self.path, 0, f"pyics test: {self.name}"


def items_for(
    session: pytest.Session,
    test: Test,
    index: int | None,
    *,
    tracing: bool = False,
    sections: Mapping[StrPath, str] | None = None,
) -> list[pytest.Item]:
    """
    The items that run ``test`` in ``session``: a ``PyicsItem``, or for a
    ``GeneralTest``, the items of the tests its source defines (see
    ``general.collect_general_test``), which all share its ``index``, the position of
    the test in the tests that are run. ``tracing`` and ``sections`` are passed on to
    the items (see ``PyicsItem``).
    """
    if isinstance(test, GeneralTest):
        items = general.collect_general_test(
            session, test, index, tracing=tracing, sections=sections
        )
    else:
        items = [
            PyicsItem.from_parent(
                session, name="", test_obj=test, tracing=tracing, sections=sections
            )
        ]
    for item in items:
        item.stash[INDEX_KEY] = index
    return items


def _merge_failure(call: TestResult, teardown: TestResult) -> TestResult:
    """The ``call`` result of a test whose ``teardown`` failed, which fails it."""
    messages = [m for m in (call.message, teardown.message) if m]
    return dataclasses.replace(
        call,
        outcome="failed",
        message="\n".join(messages) or None,
        trace=call.trace if call.trace is not None else teardown.trace,
        duration=call.duration + teardown.duration,
    )


class LiveTestCollectionPlugin:
    """
    Injects pre-built Test objects as PyicsItems, bypassing pytest's file-based collection.

    This plugin is used for the main pyics script.
    """

    def __init__(
        self,
        test_objs: list[Test],
        *,
        tracing: bool = False,
        sections: Mapping[StrPath, str] | None = None,
    ):
        """
        Parameters
        ----------
        test_objs: list[Test]
            The tests to run.
        tracing: bool (optional, keyword only)
            Trace the tests that can be traced, to show their traces if they fail (see
            ``PyicsItem``). Default is False.
        sections: Mapping[StrPath, str] | None (optional, keyword only)
            The section of each exercise notebook that the tests' task is about (see
            ``Task.sections``), which tests that weren't given a section of their own
            inherit (see ``Test.resolved_section``). Default is None, i.e. none.
        """
        self.test_objs = test_objs
        self.tracing = tracing
        self.sections = sections
        self.reports = []
        # the reports as results that can cross a process boundary, with the position
        # of their test in ``test_objs``
        self.results: list[TestResult] = []
        self._index: int | None = None
        # the position in ``results`` of the call result of the test that is running
        self._call_result: int | None = None

    def pytest_configure(self, config: pytest.Config) -> None:
        general.register(config)

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session):
        session.items = [
            item
            for i, obj in enumerate(self.test_objs)
            for item in items_for(
                session, obj, i, tracing=self.tracing, sections=self.sections
            )
        ]
        session.testscollected = len(session.items)
        return True  # short-circuits the default collection, no files are touched

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None):
        # the reports logged until the next test starts are this test's
        self._index = item.stash.get(INDEX_KEY, None)
        self._call_result = None

    def pytest_runtest_logreport(self, report: TestReport) -> None:
        """
        Record the result of each phase of a test, such that exactly one result of
        each test is its call's, which is what counts (see ``Task.update_status``): a
        setup that fails or is skipped (e.g. as the exercise file raises while a
        fixture loads it) counts as the call, which doesn't run then, and a teardown
        that fails fails the call, so that no broken test goes missing.
        """
        self.reports.append(report)
        result = TestResult.from_report(report, self._index)
        if report.when == "setup" and not report.passed:
            result = dataclasses.replace(result, when="call")
        elif report.when == "teardown" and report.failed:
            if self._call_result is not None:
                position = self._call_result
                self.results[position] = _merge_failure(self.results[position], result)
                return
            result = dataclasses.replace(result, when="call")
        if result.when == "call":
            self._call_result = len(self.results)
        self.results.append(result)


class NotebookCollectionPlugin:
    """
    Injects pre-built Test objects as PyicsItems, bypassing pytest's file-based collection.

    This plugin is meant to be used in Jupyter Notebooks, e.g. the create_task notebook.
    """

    def __init__(self, test_objs, *, tracing: bool = False):
        self.test_objs = test_objs
        self.tracing = tracing
        self.reports = []

    def pytest_configure(self, config: pytest.Config) -> None:
        general.register(config)

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        session.items = [
            item
            for i, obj in enumerate(self.test_objs)
            for item in items_for(session, obj, i, tracing=self.tracing)
        ]
        session.testscollected = len(session.items)
        return True  # short-circuits the default collection, no files are touched

    def pytest_runtest_logreport(self, report):
        self.reports.append(report)


def run_tests(
    tests: Sequence[Test],
    *,
    tracing: bool = False,
    sections: Mapping[StrPath, str] | None = None,
) -> list[TestResult]:
    """
    Run ``tests`` through pytest, as a task runs its tests, and return their results
    (see ``LiveTestCollectionPlugin``). Runs are serialized, and the terminal's mode is
    restored afterwards. ``tracing`` and ``sections`` are passed on to the plugin.
    """
    plugin = LiveTestCollectionPlugin(list(tests), tracing=tracing, sections=sections)
    with _pytest_lock, terminal.preserve_terminal_mode():
        pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
    return plugin.results
