import copy
import importlib.util as iutil
import sys
from abc import ABC, abstractmethod
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, Protocol, Self

from _pytest.assertion.util import assertrepr_compare, dummy_highlighter
from rich.markup import escape

import pyics.util.json_handling as json
from pyics.notebook import cells, loader
from pyics.tests.capture import capture_stdout, provide_input
from pyics.tests.trace import Trace
from pyics.util.paths import StrPath
from . import check_specs, check_structure, compare


class MarkupAssertionError(AssertionError):
    """
    ``AssertionError`` whose message is ``rich`` markup, which is rendered in the main
    panel. Messages of other exceptions are shown as they are.
    """


class Test(ABC):
    """Base Class for Tests"""

    name: str
    file_path: StrPath
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
        error in a cell doesn't stop the run.
        """
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
    ``;``).
    """

    expected: str  # printout is necessarily str
    inputs: list[str]
    supports_trace = True
    # whether the test may be traced, e.g. by students who want to see what their code
    # does. Also the default for tests stored before it was introduced.
    traceable: bool = True

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        expected: str,
        *,
        inputs: list[str] = [],
        traceable: bool = True,
        **kwargs,
    ):
        if isinstance(inputs, str):
            raise TypeError("inputs must be a list, with an answer per input() call")
        self.name = name
        self.file_path = file_path
        self.expected = expected
        self.inputs = [str(answer) for answer in inputs]
        self.traceable = traceable

    def run(self, trace: Trace | None = None) -> None:
        trace = trace if self.traceable else None
        with capture_stdout() as buffer, provide_input(self.inputs):
            if cells.is_notebook(self.file_path):
                # each cell's output is captured on its own
                run = loader.run_notebook(self.file_path, as_main=True, trace=trace)
                buffer.write(run.output())
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
    are named by their cell, e.g. "cell 3, line 2". Cells that can't be parsed are left
    out, and named if the test fails.
    """

    rules: list[check_structure.Rule]

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        rules: list[check_structure.Rule] | check_structure.Rule,
        **kwargs,
    ):
        self.name = name
        self.file_path = file_path
        self.rules = check_structure.validate_rules(rules)

    def run(self, trace: Trace | None = None) -> None:
        path = Path(self.file_path)
        if cells.is_notebook(path):
            diff = self._check_notebook(loader.python_source(path), path.name)
        else:
            diff = check_structure.check_structure(
                self.rules, path.read_text(encoding="utf-8"), filename=path.name
            )
        if diff:
            raise MarkupAssertionError("\n".join(diff))

    def _check_notebook(
        self, source: loader.NotebookSource, filename: str
    ) -> list[str]:
        """Messages for the rules that don't hold for the code cells ``source``, and the
        cells left out since they can't be parsed, which may be why."""
        diff = check_structure.check_structure(
            self.rules, source.source, filename=filename, locate=source.locate
        )
        if diff:
            diff += [
                f"Could not parse cell {cell.number}: {escape(err.msg)} "
                f"(line {err.lineno})."
                for cell, err in source.unparsable
            ]
        return diff


class SizeTest(Test):
    """file has a maximum line number or size"""

    def __init__(self, test_dict: dict): ...
