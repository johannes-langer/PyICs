import dataclasses
from os import PathLike
from typing import Literal

import pytest
from pytest import TestReport
from _pytest._code.code import TerminalRepr
from _pytest._io import TerminalWriter
from _pytest.nodes import Node
from rich.errors import MarkupError
from rich.style import Style
from rich.text import Text

from pyics.tests import Test
from pyics.tests.results import TestResult
from pyics.tests.tests import MarkupAssertionError
from pyics.tests.trace import Trace, ansi_text


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
        self, *, name: str, parent: Node, test_obj: Test, tracing: bool = False
    ):
        self.test_obj = test_obj
        # whether the test is traced, if it can be (see ``Test.run``), to show the trace
        # if it fails
        self.tracing = tracing
        self.recorded_trace: Trace | None = None
        super().__init__(name=self.test_obj.name, parent=parent)

    def runtest(self) -> None:
        if not (self.tracing and self.test_obj.supports_trace):
            self.test_obj.run()
            return
        # tests whose authors don't allow it ignore the trace
        self.recorded_trace = Trace()
        self.test_obj.run(trace=self.recorded_trace)

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
        return RichFailureRepr(
            Text.assemble(f"{self.name} failed: ", message), self._trace_ansi()
        )

    def _trace_ansi(self) -> str | None:
        """The trace of the failed test, or why there is none (dimmed), if it is
        traced, as text with ANSI escape codes (see ``Trace.ansi``)."""
        if not self.tracing:
            return None
        dim = Style(dim=True).render
        if not self.test_obj.supports_trace:
            return dim(f"{type(self.test_obj).__name__}s can't be traced.")
        if not getattr(self.test_obj, "traceable", True):
            return dim("Tracing is turned off for this test.")
        assert self.recorded_trace is not None
        if not (trace := self.recorded_trace.ansi):
            return dim(
                "Nothing was traced: the test failed before running your code."
            )
        return trace

    def reportinfo(self) -> tuple[PathLike | str, int | None, str]:
        return self.path, 0, f"pyics test: {self.name}"


class LiveTestCollectionPlugin:
    """
    Injects pre-built Test objects as PyicsItems, bypassing pytest's file-based collection.

    This plugin is used for the main pyics script.
    """

    def __init__(self, test_objs: list[Test], *, tracing: bool = False):
        """
        Parameters
        ----------
        test_objs: list[Test]
            The tests to run.
        tracing: bool (optional, keyword only)
            Trace the tests that can be traced, to show their traces if they fail (see
            ``PyicsItem``). Default is False.
        """
        self.test_objs = test_objs
        self.tracing = tracing
        self.reports = []
        # the reports as results that can cross a process boundary, with the position
        # of their test in ``test_objs``
        self.results: list[TestResult] = []
        self._indices: dict[int, int] = {}
        self._index: int | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session):
        session.items = [
            PyicsItem.from_parent(session, name="", test_obj=obj, tracing=self.tracing)
            for obj in self.test_objs
        ]
        session.testscollected = len(session.items)
        # by identity, since tests can share their name, and with it their nodeid
        self._indices = {id(item): i for i, item in enumerate(session.items)}
        return (
            True  # @Claude: "short-circuits the default collection, no files are touched"
        )

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None):
        # the reports logged until the next test starts are this test's
        self._index = self._indices.get(id(item))

    def pytest_runtest_logreport(self, report):
        self.reports.append(report)
        self.results.append(TestResult.from_report(report, self._index))


class NotebookCollectionPlugin:
    """
    Injects pre-built Test objects as PyicsItems, bypassing pytest's file-based collection.

    This plugin is meant to be used in Jupyter Notebooks, e.g. the create_task notebook.
    """

    def __init__(self, test_objs, *, tracing: bool = False):
        self.test_objs = test_objs
        self.tracing = tracing
        self.reports = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        session.items = [
            PyicsItem.from_parent(
                session, name=f"test_{i}", test_obj=obj, tracing=self.tracing
            )
            for i, obj in enumerate(self.test_objs)
        ]
        session.testscollected = len(session.items)
        return True  # short-circuits the default collection, no files are touched

    def pytest_runtest_logreport(self, report):
        self.reports.append(report)
