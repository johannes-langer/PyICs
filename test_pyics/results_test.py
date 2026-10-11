"""
Test results that cross a process boundary (``TestResult``): built from pytest's reports,
stored as JSON, and shown in the main panel as if the tests had run where they are
shown.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console, RenderableType
from rich.text import Text

from pyics.data import Task
from pyics.tests.pytest_integration import LiveTestCollectionPlugin, RichFailureRepr
from pyics.tests.results import TestResult, to_markup
from pyics.tests.tests import MarkupAssertionError, SimpleTest, Test
from pyics.tests.trace import Trace

SOLUTION = """
def double(x):
    return 2 * x
"""

# markup a test's author could write, with backslashes and brackets that aren't tags
# (a backslash in front of a tag is escaped by another one)
MARKUP = "expected [b]C:\\data\\\\[/b], got [red]\\[x][/red] in [i]cell [1][/i] \\"


class MarkupFailure(SimpleTest):
    """A test that fails with ``MARKUP``."""

    def run(self, trace: Trace | None = None) -> None:
        raise MarkupAssertionError(MARKUP)


@pytest.fixture
def solution(tmp_path: Path) -> Path:
    path = tmp_path / "solution.py"
    path.write_text(SOLUTION)
    return path


def _ansi(renderable: RenderableType) -> str:
    """``renderable`` as a terminal shows it, with ANSI escape codes."""
    console = Console(
        file=io.StringIO(), force_terminal=True, color_system="truecolor", width=200
    )
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]


def _run(
    tests: list[Test], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracing: bool
) -> LiveTestCollectionPlugin:
    """Run ``tests`` like a ``Task`` does."""
    monkeypatch.chdir(tmp_path)  # pytest writes its cache to the working directory
    plugin = LiveTestCollectionPlugin(tests, tracing=tracing)
    pytest.main(["-p", "no:terminal", "--capture=no"], plugins=[plugin])
    return plugin


def _round_trip(result: TestResult) -> TestResult:
    """``result`` after it was written as JSON and read again."""
    return TestResult.from_json(json.loads(json.dumps(result.to_json())))


@pytest.mark.parametrize("tracing", [False, True])
def test_results_survive_json_and_render_like_the_reports(
    solution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracing: bool
) -> None:
    tests: list[Test] = [
        SimpleTest("passes", solution, "double", 4, function_args=[2]),
        SimpleTest("fails", solution, "double", 5, function_args=[2]),
        MarkupFailure("markup", solution, "double", None),
    ]
    plugin = _run(tests, tmp_path, monkeypatch, tracing)
    results = plugin.results
    calls = [(r.nodeid, r.outcome, r.index) for r in results if r.when == "call"]
    assert calls == [
        ("::passes", "passed", 0),
        ("::fails", "failed", 1),
        ("::markup", "failed", 2),
    ]

    loaded = [_round_trip(result) for result in results]
    assert loaded == results

    # the message and the trace look the same as those of the reports
    for report, result in zip(plugin.reports, loaded, strict=True):
        if not isinstance(report.longrepr, RichFailureRepr):
            assert report.passed and result.message is None and result.trace is None
            continue
        assert _ansi(result.message_text) == _ansi(report.longrepr.text)
        trace = report.longrepr.trace
        assert (result.trace_text is None) == (trace is None) == (not tracing)
        if trace is not None:
            assert result.trace_text is not None
            assert _ansi(result.trace_text) == _ansi(trace)

    fails, markup = [r for r in loaded if r.when == "call" and r.outcome == "failed"]
    assert fails.message_text.plain == "fails failed: 4 == 5"
    assert markup.message_text.plain == (
        "markup failed: expected C:\\data\\, got [x] in cell [1] \\"
    )
    segments = Console(width=200).render(markup.message_text)
    assert [s.text for s in segments if s.style and s.style.bold] == ["C:\\data\\"]
    if tracing:
        # highlighted, as snoop highlights it
        assert fails.trace is not None and "\x1b[" in fails.trace
        assert "<<< Return value from double: 4" in (fails.trace_text or Text()).plain
        assert markup.trace_text is not None
        assert markup.trace_text.plain == (
            "Nothing was traced: the test failed before running your code."
        )


def test_the_main_panel_shows_loaded_results_as_it_shows_its_own(
    solution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests: list[Test] = [
        SimpleTest("passes", solution, "double", 4, function_args=[2]),
        SimpleTest("fails", solution, "double", 5, function_args=[2]),
        MarkupFailure("markup", solution, "double", None),
    ]
    results = _run(tests, tmp_path, monkeypatch, tracing=True).results
    task_file = Task.create_task_json(
        tmp_path / "t", "t", "", solution, solution, tests[:2]
    )
    task = Task(task_file, setup=False)

    task.test_results = results
    task.update_status()
    shown = _ansi(task.status)
    task.test_results = [_round_trip(result) for result in results]
    task.update_status()
    assert _ansi(task.status) == shown

    text = Text.from_ansi(shown).plain
    assert "fails failed: 4 == 5" in text
    assert "<<< Return value from double: 4" in text
    assert "2 failed, 1 passed" in text


def test_results_know_the_position_of_their_test(
    solution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tests with the same name have the same nodeid
    tests: list[Test] = [
        SimpleTest("same", solution, "double", 4, function_args=[2]),
        SimpleTest("same", solution, "double", 5, function_args=[2]),
    ]
    results = _run(tests, tmp_path, monkeypatch, tracing=False).results
    assert [(r.when, r.index) for r in results] == [
        ("setup", 0),
        ("call", 0),
        ("teardown", 0),
        ("setup", 1),
        ("call", 1),
        ("teardown", 1),
    ]
    assert [r.outcome for r in results if r.when == "call"] == ["passed", "failed"]


def test_failures_of_other_items_are_kept_as_plain_text() -> None:
    report = pytest.TestReport(
        "::other",
        ("other.py", 0, "other"),
        {},
        "failed",
        "boom: [b]not markup[/b] \\",
        "setup",
    )
    result = _round_trip(TestResult.from_report(report))
    assert result.message_text.plain == "boom: [b]not markup[/b] \\"
    assert result.trace is None and result.index is None


@pytest.mark.parametrize(
    "text",
    [
        Text("ends with a backslash \\"),
        Text("ends with two backslashes \\\\"),
        Text("C:\\dir\\[b]isn't a tag, nor is \\[this] or [1] or [/]"),
        Text.assemble("before \\", ("bold \\\\", "bold"), " after \\\\"),
        # adjacent spans of the same style stay apart
        Text.assemble(("one", "red"), ("two", "red"), style="dim"),
        Text.from_markup("[b]a[i]b[/b]c[/i] :smile: [#ff0000 on blue]d[/]"),
    ],
)
def test_markup_renders_like_the_text(text: Text) -> None:
    loaded = Text.from_markup(to_markup(text), emoji=False)
    assert loaded.plain == text.plain
    assert _ansi(loaded) == _ansi(text)


def test_malformed_markup_is_shown_as_it_is() -> None:
    result = TestResult("::t", "call", "failed", 0.1, message="[/] closes nothing")
    assert result.message_text.plain == "[/] closes nothing"
    assert TestResult("::t", "call", "passed", 0.1).message_text.plain == ""


VALID: dict[str, Any] = {
    "nodeid": "::t",
    "when": "call",
    "outcome": "failed",
    "duration": 0.5,
    "message": "t failed",
    "trace": None,
    "index": 3,
}


def test_stored_results_may_leave_out_what_they_dont_have() -> None:
    data = {"nodeid": "::t", "when": "call", "outcome": "passed", "duration": 1}
    assert TestResult.from_json({**data, "unknown": [1]}) == TestResult(
        "::t", "call", "passed", 1.0
    )
    assert TestResult.from_json(VALID).index == 3


@pytest.mark.parametrize(
    "change",
    [
        {"nodeid": None},
        {"when": 1},
        {"duration": "0.5"},
        {"duration": True},
        {"duration": float("nan")},
        {"message": ["t failed"]},
        {"trace": 1},
        {"index": 1.5},
        {"index": False},
    ],
)
def test_malformed_results_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TestResult.from_json({**VALID, **change})
    with pytest.raises(ValueError):
        TestResult.from_json([VALID])
