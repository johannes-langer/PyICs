import dataclasses
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import pytest
from pytest import TestReport
from _pytest._code.code import TerminalRepr
from _pytest._io import TerminalWriter
from _pytest.nodes import Node
from rich.errors import MarkupError
from rich.text import Text

from pyics.notebook import loader
from pyics.tests import Test
from pyics.tests.tests import TASK_SECTIONS, MarkupAssertionError
from pyics.tests.trace import Trace
from pyics.util import paths
from pyics.util.paths import StrPath


@dataclasses.dataclass(eq=False)
class RichFailureRepr(TerminalRepr):
    """
    Failure representation of a ``PyicsItem``, keeping the highlighting of its message
    (and its ``trace``, if the test was traced) for the main panel. Elsewhere, e.g. in
    ``TestReport.longreprtext``, it is plain text.
    """

    text: Text
    trace: Text | None = None

    def toterminal(self, tw: TerminalWriter) -> None:
        tw.line(self.text.plain)
        if self.trace is not None:
            tw.line(self.trace.plain)


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
        self.sections: Mapping[Path, str] = MappingProxyType(
            {paths.absolute(path): name for path, name in (sections or {}).items()}
        )
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
            Text.assemble(f"{self.name} failed: ", message), self._trace_text()
        )

    def _trace_text(self) -> Text | None:
        """The trace of the failed test, or why there is none, if it is traced."""
        if not self.tracing:
            return None
        if not self.test_obj.supports_trace:
            return Text(f"{type(self.test_obj).__name__}s can't be traced.", style="dim")
        if not getattr(self.test_obj, "traceable", True):
            return Text("Tracing is turned off for this test.", style="dim")
        assert self.recorded_trace is not None
        if not (trace := self.recorded_trace.text):
            return Text(
                "Nothing was traced: the test failed before running your code.",
                style="dim",
            )
        return trace

    def reportinfo(self) -> tuple[PathLike | str, int | None, str]:
        return self.path, 0, f"pyics test: {self.name}"


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

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session):
        session.items = [
            PyicsItem.from_parent(
                session,
                name="",
                test_obj=obj,
                tracing=self.tracing,
                sections=self.sections,
            )
            for obj in self.test_objs
        ]
        session.testscollected = len(session.items)
        return (
            True  # @Claude: "short-circuits the default collection, no files are touched"
        )

    def pytest_runtest_logreport(self, report):
        self.reports.append(report)


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
