"""
Test results as plain data, which can cross a process boundary: written as JSON by the
process that ran the tests (e.g. a notebook's kernel, see ``pyics.notebook.channel``),
and shown by another (the TUI), exactly as if it had run them itself.
"""

import dataclasses
import math
import re
from collections.abc import Mapping
from typing import Any, Self

import pytest
from rich.errors import MarkupError
from rich.text import Span, Text

from pyics.tests.trace import ansi_text

# Backslashes in plain text that rich's markup parser (``rich.markup.render``) would
# take away: the ones in front of what it takes for a tag (group 2), which escape it
# (one) or each other (two), and the last one in front of any other "[" (group 3)
_UNESCAPE = re.compile(r"(\\*)(\[[a-z#/@][^[]*?])|(\\+)(?=\[)")


def _escape(plain: str, *, tag_follows: bool) -> str:
    """``plain`` as markup that renders as ``plain``, followed by a tag (``tag_follows``)
    or by nothing."""

    def escape(match: re.Match[str]) -> str:
        if match[2] is not None:
            return f"{match[1]}{match[1]}\\{match[2]}"
        return f"{match[3]}\\"

    escaped = _UNESCAPE.sub(escape, plain)
    if tag_follows:
        # trailing backslashes would escape the tag after them
        escaped += "\\" * (len(escaped) - len(escaped.rstrip("\\")))
    return escaped


def to_markup(text: Text) -> str:
    """
    Console markup of ``text``, which ``Text.from_markup(markup, emoji=False)`` turns back
    into text that renders the same.

    Like ``Text.markup``, which loses backslashes in front of "[" and at the end of the
    text, and joins adjacent spans of the same style.
    """
    plain = text.plain
    spans = [Span(0, len(plain), text.style), *text.spans]
    tags: list[tuple[int, int, int, int, str]] = []
    for i, (start, end, style) in enumerate(spans):
        if not style:
            continue
        if end <= start:  # empty, but it still splits the text where it is
            tags.append((start, 1, 0, i, f"[{style}][/{style}]"))
            continue
        tags.append((start, 1, 0, i, f"[{style}]"))
        # at the same offset, spans are closed before others are opened, and in the
        # reverse order of their opening
        tags.append((end, 0, -start, -i, f"[/{style}]"))
    tags.sort(key=lambda tag: tag[:4])
    output: list[str] = []
    position = 0
    for offset, *_, tag in tags:
        if offset > position:
            output.append(_escape(plain[position:offset], tag_follows=True))
            position = offset
        output.append(tag)
    output.append(_escape(plain[position:], tag_follows=False))
    return "".join(output)


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
        of different runs of a task's tests. None if unknown.
    """

    __test__ = False  # not a test class, despite its name

    nodeid: str
    when: str
    outcome: str
    duration: float
    message: str | None = None
    trace: str | None = None
    index: int | None = None

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
        return cls(
            nodeid=_field(data, "nodeid", str),
            when=_field(data, "when", str),
            outcome=_field(data, "outcome", str),
            duration=duration,
            message=_field(data, "message", str, type(None)),
            trace=_field(data, "trace", str, type(None)),
            index=_field(data, "index", int, type(None)),
        )

    @classmethod
    def from_report(cls, report: pytest.TestReport, index: int | None = None) -> Self:
        """
        The result of the phase of a test ``report`` reports on, keeping the
        highlighting of the message and the trace of a ``PyicsItem``.

        Parameters
        ----------
        report: pytest.TestReport
            The report.
        index: int | None
            The position of the test in the tests that were run (see ``index``).
        """
        # imported here, because pytest_integration builds results with this class
        from pyics.tests.pytest_integration import RichFailureRepr

        message = trace = None
        if isinstance(report.longrepr, RichFailureRepr):
            message = to_markup(report.longrepr.text)
            trace = report.longrepr.trace_ansi
        elif report.skipped and isinstance(report.longrepr, tuple):
            # (file, line, reason) of a skipped test
            message = to_markup(Text(str(report.longrepr[-1])))
        elif not report.passed:
            message = to_markup(Text(report.longreprtext))
        return cls(
            nodeid=report.nodeid,
            when=report.when,
            outcome=report.outcome,
            duration=float(report.duration),
            message=message,
            trace=trace,
            index=index,
        )
