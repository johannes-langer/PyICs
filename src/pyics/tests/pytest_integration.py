import dataclasses
from os import PathLike
from typing import Literal

import pytest
from pytest import TestReport
from _pytest._code.code import TerminalRepr
from _pytest._io import TerminalWriter
from _pytest.nodes import Node
from rich.errors import MarkupError
from rich.text import Text

from pyics.tests import Test
from pyics.tests.tests import MarkupAssertionError


@dataclasses.dataclass(eq=False)
class RichFailureRepr(TerminalRepr):
    """
    Failure representation of a ``PyicsItem``, keeping the highlighting of its message
    for the main panel. Elsewhere, e.g. in ``TestReport.longreprtext``, it is plain text.
    """

    text: Text

    def toterminal(self, tw: TerminalWriter) -> None:
        tw.line(self.text.plain)


class PyicsItem(pytest.Item):
    """
    pytest.Item intended to wrap the tests below for access with pytest

    """

    def __init__(self, *, name: str, parent: Node, test_obj: Test):
        self.test_obj = test_obj
        super().__init__(name=self.test_obj.name, parent=parent)

    def runtest(self) -> None:
        self.test_obj.run()

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
        return RichFailureRepr(Text.assemble(f"{self.name} failed: ", message))

    def reportinfo(self) -> tuple[PathLike | str, int | None, str]:
        return self.path, 0, f"pyics test: {self.name}"


class LiveTestCollectionPlugin:
    """
    Injects pre-built Test objects as PyicsItems, bypassing pytest's file-based collection.

    This plugin is used for the main pyics script.
    """

    def __init__(self, test_objs: list[Test]):
        self.test_objs = test_objs
        self.reports = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session):
        session.items = [
            PyicsItem.from_parent(session, name="", test_obj=obj) for obj in self.test_objs
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

    def __init__(self, test_objs):
        self.test_objs = test_objs
        self.reports = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        session.items = [
            PyicsItem.from_parent(session, name=f"test_{i}", test_obj=obj)
            for i, obj in enumerate(self.test_objs)
        ]
        session.testscollected = len(session.items)
        return True  # short-circuits the default collection, no files are touched

    def pytest_runtest_logreport(self, report):
        self.reports.append(report)
