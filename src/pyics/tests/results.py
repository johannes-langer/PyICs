"""
Test results as plain data, which can cross a process boundary: written as JSON by the
process that ran the tests (e.g. a notebook's kernel, see ``pyics.plugins``), and shown
by another (the TUI) as if it had run them itself.
"""

import dataclasses
import math
from collections.abc import Mapping
from typing import Any, Self

import pytest
from rich.errors import MarkupError
from rich.markup import escape
from rich.text import Text

from pyics.tests.trace import ansi_text

def _field(data: Mapping[str, Any], name: str, *types: type) -> Any:
    """``data[name]``, which must be of one of ``types`` (a missing key counts as None).
    JSON's ``true`` and ``false`` don't count as numbers."""
    value = data.get(name)
    if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
        raise ValueError(f"{name!r} must be {' or '.join(t.__name__ for t in types)}")
    return value


@dataclasses.dataclass(frozen=True)
class TestResult:
    """
    The result of one phase (setup, call or teardown) of one test, as the main panel
    shows it (see ``Task.update_status``), in a form that can be stored as JSON (see
    ``to_json`` and ``from_json``).

    Attributes
    ----------
    nodeid: str
        pytest's id of the test, e.g. "::adds".
    when: str
        The phase: "setup", "call" or "teardown".
    outcome: str
        "passed", "failed" or "skipped".
    duration: float
        How long the phase took, in seconds.
    message: str | None
        Why the test failed or was skipped, as rich console markup, e.g. "adds failed:
        3 == 4" (see ``message_text``). None if it passed.
    trace: str | None
        If the test failed while it was traced: its trace, or a line saying why there
        is none, as text with ANSI escape codes (see ``Trace.ansi`` and
        ``trace_text``). None if it wasn't traced.
    index: int | None
        The position of the test in the tests that were run, e.g. to match the results
        of different runs of a task's tests. None if unknown. The results of all tests
        that a ``GeneralTest`` collects share its index.
    soft: bool
        Whether the test is soft (see ``Test.soft``): if it failed, it is a warning,
        which doesn't keep its task from counting as solved. False for results stored
        before it was introduced.
    """

    __test__ = False  # not a test class, despite its name

    nodeid: str
    when: str
    outcome: str
    duration: float
    message: str | None = None
    trace: str | None = None
    index: int | None = None
    soft: bool = False

    @property
    def message_text(self) -> Text:
        """``message`` as highlighted text, empty if there is none. Malformed markup is
        shown as it is."""
        if self.message is None:
            return Text()
        try:
            return Text.from_markup(self.message, emoji=False)
        except MarkupError:
            return Text(self.message)

    @property
    def trace_text(self) -> Text | None:
        """``trace`` as highlighted text, or None if there is none."""
        return None if self.trace is None else ansi_text(self.trace)

    def to_json(self) -> dict[str, Any]:
        """The result as a dict of JSON types, the inverse of ``from_json``."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> Self:
        """
        The result stored as ``data`` by ``to_json``. Keys it doesn't know are ignored.

        Raises
        ------
        ValueError
            If ``data`` isn't a stored result.
        """
        if not isinstance(data, Mapping):
            raise ValueError("a test result must be an object")
        duration = float(_field(data, "duration", int, float))
        if not math.isfinite(duration):
            raise ValueError("'duration' must be finite")
        # stored only since soft tests were introduced
        soft = _field(data, "soft", bool) if "soft" in data else False
        return cls(
            nodeid=_field(data, "nodeid", str),
            when=_field(data, "when", str),
            outcome=_field(data, "outcome", str),
            duration=duration,
            message=_field(data, "message", str, type(None)),
            trace=_field(data, "trace", str, type(None)),
            index=_field(data, "index", int, type(None)),
            soft=soft,
        )

    @classmethod
    def from_report(
        cls, report: pytest.TestReport, index: int | None = None, *, soft: bool = False
    ) -> Self:
        """
        The result of the phase of a test ``report`` reports on, keeping the
        highlighting of the message and the trace of a ``PyicsItem``.

        Parameters
        ----------
        report: pytest.TestReport
            The report.
        index: int | None
            The position of the test in the tests that were run (see ``index``).
        soft: bool (optional, keyword only)
            Whether the test is soft (see ``soft``). Default is False.
        """
        # imported here, because pytest_integration builds results with this class
        from pyics.tests.pytest_integration import RichFailureRepr

        message = trace = None
        if isinstance(report.longrepr, RichFailureRepr):
            message = report.longrepr.text.markup
            trace = report.longrepr.trace_ansi
        elif report.skipped and isinstance(report.longrepr, tuple):
            # (file, line, reason) of a skipped test
            message = escape(str(report.longrepr[-1]))
        elif not report.passed:
            message = escape(report.longreprtext)
        return cls(
            nodeid=report.nodeid,
            when=report.when,
            outcome=report.outcome,
            duration=float(report.duration),
            message=message,
            trace=trace,
            index=index,
            soft=soft,
        )
