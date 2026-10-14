"""
Running ``GeneralTest``s, whose tests are pytest code: the collector that collects the
tests of their source (see ``collect_general_test``), the fixtures their tests can use,
and the hooks that show their failures like those of the other tests.

Fixtures, for the tests of a ``GeneralTest`` only:

- ``solution``: the exercise file loaded as a module, anew for each test (see
  ``Test.load_solution``), or the code that isn't saved yet in a live run (see
  ``LIVE_SOURCE``).
- ``load_solution``: a function that loads the exercise file anew on every call, e.g.
  to check that a function doesn't keep state between runs. Skipped in live runs.
- ``run_solution(inputs=(), section=INHERIT)``: runs the exercise file as a script, as
  a ``PrintTest`` does, and returns its output (see ``run_script``). Skipped in live
  runs.
- ``exercise_path``: the absolute path of the exercise file.
- ``capsys``: captures what the test's own thread writes to stdout and stderr, unlike
  pytest's, which would capture the TUI's output, too.

This module is registered as a plugin by the collection plugins of
``pytest_integration`` (see ``register``).
"""

import ast
import copy
import re
import sys
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Literal

import pytest
from _pytest._code.code import TerminalRepr
from _pytest.assertion.rewrite import rewrite_asserts
from _pytest.capture import CaptureResult
from _pytest.nodes import Collector, Node
from _pytest.runner import collect_one_node
from rich.text import Text

from pyics.notebook import loader
from pyics.tests import capture
from pyics.tests.tests import (
    INHERIT,
    TASK_SECTIONS,
    GeneralTest,
    Inherit,
    _skip_live_run,
    run_script,
)
from pyics.tests.trace import Trace
from pyics.util import paths
from pyics.util.paths import StrPath

PLUGIN_NAME = "pyics-general-tests"

# the module the source of a ``GeneralTest`` runs as (see ``SourceModule``)
MODULE_NAME = "pyics_general_test"

# the trace of a test of a ``GeneralTest`` that is traced, by item
TRACE_KEY = pytest.StashKey[Trace | None]()


def register(config: pytest.Config) -> None:
    """Register this module as a plugin of ``config``, unless it is already."""
    module = sys.modules[__name__]
    if not config.pluginmanager.is_registered(module):
        config.pluginmanager.register(module, PLUGIN_NAME)


def absolute_sections(sections: Mapping[StrPath, str] | None) -> Mapping[Path, str]:
    """``sections``, the sections of a task by notebook, by absolute path (see
    ``TASK_SECTIONS``)."""
    return MappingProxyType(
        {paths.absolute(path): name for path, name in (sections or {}).items()}
    )


class SourceModule(pytest.Module):
    """
    The source of a ``GeneralTest`` as a pytest module, collected like a test file:
    its assertions are rewritten, so that failures explain themselves as in pytest, and
    its lines are registered in ``linecache``, so that tracebacks show them.
    """

    def __init__(
        self,
        *,
        test: GeneralTest,
        tracing: bool = False,
        sections: Mapping[StrPath, str] | None = None,
        **kwargs: Any,
    ):
        # with an absolute path, as its tests may change the working directory, e.g. to
        # a temporary one
        self.test = copy.copy(test)
        self.test.file_path = paths.absolute(test.file_path)
        # whether its tests are traced, if the test's author allows it
        self.tracing = tracing
        # the sections of its task, which its tests inherit (see ``TASK_SECTIONS``)
        self.sections = absolute_sections(sections)
        self._module: ModuleType | None = None
        super().__init__(**kwargs)

    def _getobj(self) -> ModuleType:
        # cached, since collecting calls it more than once
        if self._module is None:
            self._module = self._load()
        return self._module

    def _load(self) -> ModuleType:
        # compiled under its name alone, which tracebacks show as it is, as there is no
        # such file (see ``_virtual_path``)
        filename = self.path.name
        source = self.test.source
        try:
            tree = ast.parse(source, filename)
        except SyntaxError as err:
            raise Collector.CollectError(
                f"The test's source can't be parsed: {err.msg} (line {err.lineno})"
            ) from err
        rewrite_asserts(tree, source.encode(), filename, self.config)
        loader.register_source(filename, source)
        code = compile(tree, filename, "exec", dont_inherit=True)
        module = ModuleType(MODULE_NAME)
        module.__file__ = filename
        # e.g. dataclasses look up the module of their class
        sys.modules[MODULE_NAME] = module
        exec(code, module.__dict__)  # noqa: S102
        return module


class _CollectionFailure(pytest.Item):
    """Stands in for the tests of a ``GeneralTest`` whose source can't be collected,
    or has no tests, failing with ``message``, so that the test can't go missing."""

    def __init__(self, *, test: GeneralTest, message: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.test = test
        self.message = message

    def runtest(self) -> None:
        raise AssertionError(self.message)

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: Any = None,
    ) -> str | TerminalRepr:
        # imported here, because pytest_integration imports this module
        from pyics.tests.pytest_integration import RichFailureRepr

        return RichFailureRepr(Text(f"{self.name} failed: {self.message}"))

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.path, 0, f"pyics test: {self.name}"


def _virtual_path(test: GeneralTest, index: int | None) -> Path:
    """The path of the source of ``test``, next to its exercise file. Its name, under
    which the source is registered in ``linecache``, is unique among the tests of a
    run (by its ``index``), so that tracebacks show the right lines."""
    slug = re.sub(r"\W+", "_", test.name).strip("_") or "test"
    suffix = "" if index is None else f"_{index}"
    return paths.absolute(test.file_path).parent / f"{slug}{suffix}.pyicstest.py"


def collect_general_test(
    session: pytest.Session,
    test: GeneralTest,
    index: int | None = None,
    *,
    tracing: bool = False,
    sections: Mapping[StrPath, str] | None = None,
) -> list[pytest.Item]:
    """
    The items of the tests that the source of ``test`` defines, e.g. one per case of a
    parametrized test, with nodeids such as ``::name::test_x[case]``. If the source
    can't be collected, or has no tests, an item that fails with the error stands in
    for them.

    Parameters
    ----------
    session: pytest.Session
        The session the items run in.
    test: GeneralTest
        The test.
    index: int | None
        The position of the test in the tests that are run, which makes its path
        unique (see ``_virtual_path``).
    tracing: bool (optional, keyword only)
        Trace the tests, if the test's author allows it. Default is False.
    sections: Mapping[StrPath, str] | None (optional, keyword only)
        The sections of its task, which its tests inherit (see ``TASK_SECTIONS``).
    """
    module = SourceModule.from_parent(
        session,
        path=_virtual_path(test, index),
        nodeid=f"::{test.name}",
        test=test,
        tracing=tracing,
        sections=sections,
    )
    items: list[pytest.Item] = []
    errors: list[str] = []

    # node by node, rather than by ``session.genitems``, where a collection error
    # would stop the whole run
    def collect(collector: Collector) -> None:
        report = collect_one_node(collector)
        if report.failed:
            errors.append(report.longreprtext.strip())
            return
        for node in report.result:
            if isinstance(node, pytest.Item):
                items.append(node)
            elif isinstance(node, Collector):
                collect(node)

    collect(module)
    if errors or not items:
        message = (
            "\n".join(errors)
            if errors
            else "The test's source has no tests: functions named test... or classes "
            "named Test..."
        )
        items.append(
            _CollectionFailure.from_parent(
                session, name=test.name, test=test, message=message
            )
        )
    return items


def source_module(node: Node) -> SourceModule | None:
    """The ``SourceModule`` that ``node`` was collected from, if any."""
    return node.getparent(SourceModule)


def general_test(node: Node) -> GeneralTest | None:
    """The ``GeneralTest`` of ``node``, an item of its tests, if it is one."""
    if isinstance(node, _CollectionFailure):
        return node.test
    module = source_module(node)
    return None if module is None else module.test


def _module_of(request: pytest.FixtureRequest) -> SourceModule:
    module = source_module(request.node)
    if module is None:
        pytest.fail(
            f"the fixture {request.fixturename!r} is only for GeneralTests",
            pytrace=False,
        )
    return module


@contextmanager
def _untraced() -> Generator[None]:
    """Stop tracing in this context, e.g. while a module is loaded by a traced test,
    as importing a module isn't traced (see ``Test.load_solution``)."""
    previous = sys.gettrace()
    sys.settrace(None)
    try:
        yield
    finally:
        sys.settrace(previous)


# --- fixtures ---


@pytest.fixture
def solution(request: pytest.FixtureRequest) -> ModuleType:
    """The exercise file, loaded as a module for this test (see
    ``Test.load_solution``)."""
    __tracebackhide__ = True  # the traceback starts in the exercise file
    test = _module_of(request).test
    return test.load_solution(trace=request.node.stash.get(TRACE_KEY, None))


@pytest.fixture
def load_solution(request: pytest.FixtureRequest) -> Callable[[], ModuleType]:
    """A function that loads the exercise file as a new module on every call. Skipped
    in live runs (see ``LIVE_SOURCE``)."""
    test = _module_of(request).test
    _skip_live_run()
    trace = request.node.stash.get(TRACE_KEY, None)

    def load() -> ModuleType:
        __tracebackhide__ = True
        with _untraced():
            return test.load_solution(trace=trace)

    return load


@pytest.fixture
def run_solution(request: pytest.FixtureRequest) -> Callable[..., str]:
    """A function that runs the exercise file as a script, answering its ``input()``
    calls with ``inputs``, and returns its output, as a ``PrintTest`` runs it (see
    ``run_script``). Skipped in live runs (see ``LIVE_SOURCE``)."""
    test = _module_of(request).test
    _skip_live_run()
    trace = request.node.stash.get(TRACE_KEY, None)

    def run(inputs: Iterable[str] = (), section: str | None | Inherit = INHERIT) -> str:
        __tracebackhide__ = True
        if section is INHERIT:
            section = TASK_SECTIONS.get().get(paths.absolute(test.file_path))
        return run_script(test, inputs, section=section, trace=trace)

    return run


@pytest.fixture
def exercise_path(request: pytest.FixtureRequest) -> Path:
    """The absolute path of the exercise file."""
    return paths.absolute(_module_of(request).test.file_path)


class ThreadCapture:
    """
    What the thread of a test wrote to stdout and stderr (see ``readouterr``), like
    pytest's ``capsys``, which would capture the output of all threads, e.g. the TUI's.
    """

    def __init__(self, out: Any, err: Any):
        self._out = out
        self._err = err

    def readouterr(self) -> CaptureResult[str]:
        """What was written since the test started, or since the last call, as
        ``(out, err)``."""
        out, err = self._out.getvalue(), self._err.getvalue()
        for buffer in (self._out, self._err):
            buffer.seek(0)
            buffer.truncate()
        return CaptureResult(out, err)


@pytest.fixture
def capsys(request: pytest.FixtureRequest) -> Generator[ThreadCapture]:
    """Capture what this test's thread writes to stdout and stderr (see
    ``ThreadCapture``)."""
    with capture.capture_stdout() as out, capture.capture_stderr() as err:
        yield ThreadCapture(out, err)


@pytest.fixture(autouse=True)
def _pyics_isolation(request: pytest.FixtureRequest) -> Generator[None]:
    """Run each test of an ``isolated`` ``GeneralTest`` in a temporary working
    directory of its own (see ``Test._isolation``)."""
    module = source_module(request.node)
    if module is None:
        yield
        return
    with module.test._isolation():
        yield


# --- hooks ---


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, object]:
    module = source_module(item)
    if module is None:
        return (yield)
    task_sections = TASK_SECTIONS.set(module.sections)
    try:
        return (yield)
    finally:
        TASK_SECTIONS.reset(task_sections)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if (module := source_module(item)) is None:
        return
    loader.clear_last_errors()
    traced = module.tracing and module.test.traceable
    item.stash[TRACE_KEY] = Trace() if traced else None


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Generator[None, object, object]:
    trace = pyfuncitem.stash.get(TRACE_KEY, None)
    if trace is None:
        return (yield)
    with trace.active():
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    module = source_module(item)
    if module is None or not report.failed or call.excinfo is None:
        return report
    # imported here, because pytest_integration imports this module
    from pyics.tests.pytest_integration import RichFailureRepr, trace_ansi

    # pytest's short traceback, as ``--tb=short`` would show it, which pytest doesn't
    # accept without its terminal plugin
    style: Literal["short"] = "short"
    text = Text(str(item._repr_failure_py(call.excinfo, style=style)).rstrip())
    for error in loader.last_errors():  # which may be why the test failed
        text = Text.assemble(text, "\n", error.describe())
    trace = item.stash.get(TRACE_KEY, None)
    report.longrepr = RichFailureRepr(
        text, trace_ansi(module.test, tracing=module.tracing, trace=trace)
    )
    return report
