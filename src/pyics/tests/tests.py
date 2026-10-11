import copy
import enum
import importlib.util as iutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextvars import ContextVar
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, ClassVar, Literal, Protocol, Self

import pytest
from _pytest.assertion.util import assertrepr_compare, dummy_highlighter
from rich.markup import escape

import pyics.util.json_handling as json
from pyics.notebook import cells, loader, sections
from pyics.tests.capture import capture_stdout, provide_input
from pyics.tests.trace import Trace
from pyics.util import paths
from pyics.util.paths import StrPath
from . import check_specs, check_structure, compare


class _Inherit(enum.Enum):
    INHERIT = "inherit"


# the section of a test that wasn't given one (see ``PrintTest`` and ``StructureTest``):
# it inherits its task's section for its file (see ``TASK_SECTIONS``)
INHERIT = _Inherit.INHERIT
type Inherit = Literal[_Inherit.INHERIT]

# The section of each exercise notebook that the task whose tests are running is about
# (see ``Task.sections``), by the notebook's absolute path. Set while a test runs (see
# ``PyicsItem.runtest``), so that tests that weren't given a section of their own
# inherit their task's (see ``Test.resolved_section``), without storing it.
TASK_SECTIONS: ContextVar[Mapping[Path, str]] = ContextVar(
    "TASK_SECTIONS", default=MappingProxyType({})
)


class SolutionSource(Protocol):
    """
    Where tests take the code under test from in a live run (see ``SOLUTION_SOURCE``),
    rather than from a fresh run of the saved exercise file: e.g. a notebook's kernel,
    whose objects are tested as they are currently defined (see
    ``pyics.notebook.extension``).
    """

    def covers(self, path: StrPath) -> bool:
        """Whether this source holds the code of the exercise file at ``path``. Tests
        of other files load them as usual."""
        ...

    def module(self, trace: Trace | None = None) -> ModuleType:
        """
        The code under test as a module, e.g. a view of the kernel's namespace. With
        ``trace``, the code defined in it is watched by it (see ``Trace.watch``).
        """
        ...

    def python_source(
        self, path: StrPath, *, section: str | None = None
    ) -> loader.NotebookSource:
        """
        The code cells of the notebook at ``path`` as they were last run, like
        ``loader.python_source`` gives them as they were saved: of its section
        ``section`` only, if given.

        Raises
        ------
        SectionNotFound
            If the notebook has no section ``section``.
        """
        ...


# Set while the tests of a live run run (e.g. by the kernel extension, see
# ``pyics.notebook.extension``): the tests of the files it covers test its code rather
# than that of the saved files (see ``Test.load_solution`` and ``StructureTest``), and
# ``PrintTest``s are skipped, as they need a fresh run of the whole file.
SOLUTION_SOURCE: ContextVar[SolutionSource | None] = ContextVar(
    "SOLUTION_SOURCE", default=None
)


def _live_source(path: StrPath) -> SolutionSource | None:
    """The source of the live run that covers the file at ``path``, if one is
    running."""
    source = SOLUTION_SOURCE.get()
    return source if source is not None and source.covers(path) else None


class MarkupAssertionError(AssertionError):
    """
    ``AssertionError`` whose message is ``rich`` markup, which is rendered in the main
    panel. Messages of other exceptions are shown as they are.
    """


class Test(ABC):
    """Base Class for Tests"""

    name: str
    file_path: StrPath
    # the section of the notebook ``file_path`` the test is about (see
    # ``resolved_section``): None for the whole notebook, which is all that tests of
    # other kinds than ``PrintTest`` and ``StructureTest`` are about
    section: str | None | Inherit = None
    # whether the test runs the code in ``file_path``, which can then be traced (see
    # ``run``)
    supports_trace: ClassVar[bool] = False

    @abstractmethod
    def __init__(self): ...

    @classmethod
    def from_dict(cls, test_dict: dict) -> Self:
        return cls(**test_dict)

    def __call__(self) -> None:
        self.run()

    def load_solution(
        self, *, as_main: bool = False, trace: Trace | None = None
    ) -> ModuleType:
        """
        Run the file at ``file_path`` as a new module, and return it. With ``as_main``,
        it is run as a script, i.e. with ``__name__ == "__main__"``.

        With ``trace``, the file's code is watched by it (see ``Trace.watch``), so that
        the calls into it that are run by ``trace`` are traced. A script is traced as a
        whole, while importing a module isn't.

        A notebook's code cells are run instead (see ``loader.run_notebook``), where an
        error in a cell doesn't stop the run. In a live run that covers the file (see
        ``SOLUTION_SOURCE``), its code is taken from there instead.
        """
        if (source := _live_source(self.file_path)) is not None:
            return source.module(trace)
        if cells.is_notebook(self.file_path):
            return loader.load_module(self.file_path, as_main=as_main, trace=trace)

        spec: ModuleSpec | None = iutil.spec_from_file_location(
            name="current_solution", location=self.file_path
        )
        if not isinstance(spec, ModuleSpec):
            raise ImportError("Could not import solution file. Has the file been moved?")
        if spec.loader is None:
            raise AttributeError("spec.loader cannot be None.")

        module = iutil.module_from_spec(spec)
        if as_main:
            module.__name__ = "__main__"
        sys.modules["current_solution"] = module
        # compiled from source rather than run by the loader, whose cached bytecode is
        # stale if the file was saved twice within a second without changing its size
        source = Path(self.file_path).read_bytes()
        code = compile(source, self.file_path, "exec", dont_inherit=True)
        if trace is not None:
            trace.watch(code)
        if trace is not None and as_main:
            trace.run(lambda: exec(code, module.__dict__))  # noqa: S102
        else:
            exec(code, module.__dict__)  # noqa: S102

        return module

    def resolved_section(self) -> str | None:
        """
        The name of the section of the notebook ``file_path`` the test is about (see
        ``pyics.notebook.sections``), or None for the whole notebook: its own
        ``section``, or if it wasn't given one, its task's section for its file while
        the task's tests run (see ``TASK_SECTIONS``).
        """
        if self.section is INHERIT:
            return TASK_SECTIONS.get().get(paths.absolute(self.file_path))
        return self.section

    def serialize(self) -> str:
        """turn this test object into a dictionary for export"""
        return json.dumps(self)

    @abstractmethod
    def run(self, trace: Trace | None = None) -> None:
        """
        Run the test, raising an ``AssertionError`` if it fails. With ``trace``, the code
        under test is traced by it, if the test ``supports_trace`` and the test's author
        allows it.
        """


def _visible(output: str) -> str:
    """``output`` without the whitespace that isn't visible in a console: at the end of
    lines, and blank lines at the end."""
    return "\n".join(line.rstrip() for line in output.split("\n")).rstrip("\n")


class PrintTest(Test):
    """
    Console output of a file must match the desired output.

    The file is run as a script (with ``__name__ == "__main__"``) until it ends or exits,
    e.g. by calling ``sys.exit()``. Its ``input()`` calls are answered with ``inputs``,
    in order, which show up in the output like answers typed into a console. Whitespace
    that isn't visible in a console is ignored. Only the output of the thread running the
    test is captured, so that other threads (e.g. the CLI's) can keep writing to the
    console in the meantime.

    A notebook's code cells are run instead, and their output is compared, which
    includes what Jupyter shows as text: the ``repr`` of objects passed to ``display``,
    and of each cell's final expression (unless it is ``None``, or the cell ends with
    ``;``). With a ``section`` (see ``Test.resolved_section``), the whole notebook is
    run, but only the output of the section's cells is compared. A test that isn't
    given a ``section`` inherits its task's; None compares the output of all cells.

    In a live run (see ``SOLUTION_SOURCE``), it is skipped.
    """

    expected: str  # printout is necessarily str
    inputs: list[str]
    supports_trace = True
    # whether the test may be traced, e.g. by students who want to see what their code
    # does. Also the default for tests stored before it was introduced.
    traceable: bool = True
    # only set if the test is given a section, so that tests that inherit their task's
    # section store nothing (and tests stored before sections inherit it, too)
    section: str | None | Inherit = INHERIT

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        expected: str,
        *,
        inputs: list[str] = [],
        traceable: bool = True,
        section: str | None | Inherit = INHERIT,
        **kwargs,
    ):
        if isinstance(inputs, str):
            raise TypeError("inputs must be a list, with an answer per input() call")
        self.name = name
        self.file_path = file_path
        self.expected = expected
        self.inputs = [str(answer) for answer in inputs]
        self.traceable = traceable
        if section is not INHERIT:
            self.section = None if section is None else sections.check_name(section)

    def run(self, trace: Trace | None = None) -> None:
        if SOLUTION_SOURCE.get() is not None:
            # a live run: the output needs a fresh run of the whole file
            pytest.skip("runs when the notebook is saved")
        trace = trace if self.traceable else None
        with capture_stdout() as buffer, provide_input(self.inputs):
            if cells.is_notebook(self.file_path):
                if (section := self.resolved_section()) is not None:
                    # a missing section fails the test before the notebook is run
                    sections.get_section(cells.read(self.file_path), section)
                # each cell's output is captured on its own
                run = loader.run_notebook(self.file_path, as_main=True, trace=trace)
                buffer.write(run.output(section=section))
            else:
                try:
                    self.load_solution(as_main=True, trace=trace)
                except SystemExit:
                    pass  # the program ended
        res, expected = _visible(buffer.getvalue()), _visible(self.expected)

        if res != expected:
            expl = list(
                assertrepr_compare(
                    "==",
                    res,
                    expected,
                    verbose=1,
                    highlighter=dummy_highlighter,
                    assertion_text_diff_style="ndiff",
                )
            ) or [f"{res!r} == {expected!r}"]

            raise AssertionError("\n".join(expl))


class SimpleTest(Test):
    """
    Output of a function must match the desired output for some parameters.

    The output is compared with ``compare.equal``: by ``==``, except for numpy arrays and
    pandas objects, which are compared as a whole.
    """

    function_name: str
    function_args: list[Any]
    function_kwargs: dict[str, Any]
    expected: Any
    supports_trace = True
    # whether the test may be traced, which shows its arguments, too. Also the default
    # for tests stored before it was introduced.
    traceable: bool = True

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        function_name: str,
        expected: Any,
        *,
        function_args: list[Any] = [],
        function_kwargs: dict[str, Any] = {},
        traceable: bool = True,
        **kwargs,
    ):
        self.name = name
        self.file_path = file_path
        self.function_name = function_name
        self.expected = expected
        self.function_args = function_args
        self.function_kwargs = function_kwargs
        self.traceable = traceable

    def run(self, trace: Trace | None = None) -> None:
        trace = trace if self.traceable else None
        m = self.load_solution(trace=trace)
        func = getattr(m, self.function_name)
        # copied, so a solution that modifies its arguments can't change them for the
        # next run of this test
        args = copy.deepcopy(self.function_args)
        kwargs = copy.deepcopy(self.function_kwargs)
        if trace is None:
            res = func(*args, **kwargs)
        else:
            res = trace.run(lambda: func(*args, **kwargs))

        if not compare.equal(res, self.expected):
            raise AssertionError("\n".join(compare.explain(res, self.expected)))


class SpecTest(Test):
    """
    Symbols in file match some specification.

    ``expected`` is either a spec dict (see ``check_specs``), or a class or function,
    which is converted into a spec dict so that the test can be stored as json. If
    ``by_type`` is set, parameters and attributes are matched by their type only, ignoring
    their names and order.
    """

    symbol_name: str
    expected: check_specs.SpecDict
    by_type: bool

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        symbol_name: str,
        expected: check_specs.SpecType,
        *,
        by_type: bool = False,
        **kwargs,
    ):
        self.name = name
        self.file_path = file_path
        self.symbol_name = symbol_name
        self.expected = check_specs.to_spec_dict(expected)
        self.by_type = by_type

    def run(self, trace: Trace | None = None) -> None:
        m = self.load_solution()
        symb = getattr(m, self.symbol_name)

        if diff := check_specs.compare_spec(self.expected, symb, by_type=self.by_type):
            raise MarkupAssertionError("\n".join(diff))


class StructureTest(Test):
    """
    Source code uses (or avoids) constructs such as loops or recursion.

    ``rules`` is a rule dict or a list of rule dicts (see ``check_structure``), e.g.
    ``{"concept": "recursion", "scope": "fib"}``. The file is parsed, but not executed.

    Of a notebook, the code cells are checked (see ``loader.python_source``), and lines
    are named by their cell, e.g. "cell 3, line 2". With a ``section`` (see
    ``Test.resolved_section``), only the code cells of that section are checked, and
    the test fails if one of them can't be parsed. A test that isn't given a
    ``section`` inherits its task's; None checks all code cells, leaving out those that
    can't be parsed (they are named if the test fails). In a live run that covers the
    notebook (see ``SOLUTION_SOURCE``), the cells are checked as they were last run.
    """

    rules: list[check_structure.Rule]
    # only set if the test is given a section, so that tests that inherit their task's
    # section store nothing (and tests stored before sections inherit it, too)
    section: str | None | Inherit = INHERIT

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        rules: list[check_structure.Rule] | check_structure.Rule,
        *,
        section: str | None | Inherit = INHERIT,
        **kwargs,
    ):
        self.name = name
        self.file_path = file_path
        self.rules = check_structure.validate_rules(rules)
        if section is not INHERIT:
            self.section = None if section is None else sections.check_name(section)

    def run(self, trace: Trace | None = None) -> None:
        path = Path(self.file_path)
        if cells.is_notebook(path):
            section = self.resolved_section()
            # in a live run, the cells as they were last run
            source = (_live_source(path) or loader).python_source(path, section=section)
            diff = self._check_notebook(source, path.name, whole=section is None)
        else:
            diff = check_structure.check_structure(
                self.rules, path.read_text(encoding="utf-8"), filename=path.name
            )
        if diff:
            raise MarkupAssertionError("\n".join(diff))

    def _check_notebook(
        self, source: loader.NotebookSource, filename: str, *, whole: bool = True
    ) -> list[str]:
        """
        Messages for the rules that don't hold for the code cells ``source``. Of the
        ``whole`` notebook, the cells left out since they can't be parsed are named if
        a rule doesn't hold, as that may be why. Of a section, they fail the test, like
        a Python file that can't be parsed.
        """
        unparsable = [
            f"Could not parse cell {cell.number}: {escape(err.msg)} "
            f"(line {err.lineno})."
            for cell, err in source.unparsable
        ]
        if unparsable and not whole:
            return unparsable
        diff = check_structure.check_structure(
            self.rules, source.source, filename=filename, locate=source.locate
        )
        return diff + unparsable if diff else []


class SizeTest(Test):
    """file has a maximum line number or size"""

    def __init__(self, test_dict: dict): ...
