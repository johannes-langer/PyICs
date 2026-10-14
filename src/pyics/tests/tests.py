import ast
import builtins
import contextlib
import copy
import enum
import functools
import importlib.util as iutil
import reprlib
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterable, Mapping
from contextvars import ContextVar
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, ClassVar, Literal, NamedTuple, NoReturn, Protocol, Self

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


class LiveSource(Protocol):
    """
    Where tests take the code under test from while they run on code that isn't saved
    yet (see ``LIVE_SOURCE``), e.g. on the objects a notebook's kernel defines.
    """

    def module(self, path: StrPath) -> ModuleType | None:
        """The code of the exercise file at ``path`` as a module, or None if this
        source doesn't hold it, so that the file is loaded as usual."""
        ...


# Set by a plugin while the tests run on code that isn't saved yet (see ``LiveSource``):
# ``Test.load_solution`` takes the code of the files it holds from it, and
# ``PrintTest``s and ``StructureTest``s are skipped, as they run on the saved file.
LIVE_SOURCE: ContextVar[LiveSource | None] = ContextVar("LIVE_SOURCE", default=None)


def _skip_live_run() -> None:
    """Skip the running test if it runs on code that isn't saved yet (see
    ``LIVE_SOURCE``)."""
    if LIVE_SOURCE.get() is not None:
        pytest.skip("runs when the notebook is saved")


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
        error in a cell doesn't stop the run. While a ``LIVE_SOURCE`` that holds the
        file is set, its module is returned instead, untraced.
        """
        __tracebackhide__ = True  # tracebacks of a ``GeneralTest`` start in the file
        if (source := LIVE_SOURCE.get()) is not None and (
            module := source.module(self.file_path)
        ) is not None:
            return module
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

    @contextlib.contextmanager
    def _isolation(self) -> Generator[None]:
        """
        Run the code in this context in a temporary working directory of its own, which
        is deleted afterwards, if the test is ``isolated``, e.g. so that the files the
        code under test writes don't pile up. ``file_path`` is made absolute in the
        meantime, as it may be relative to the working directory. Does nothing for
        tests that aren't isolated.
        """
        if not getattr(self, "isolated", False):
            yield
            return
        file_path = self.file_path
        self.file_path = paths.absolute(file_path)
        try:
            # cleanup errors are ignored, e.g. a file that is still open on Windows
            with (
                tempfile.TemporaryDirectory(
                    prefix="pyics-", ignore_cleanup_errors=True
                ) as tmp,
                contextlib.chdir(tmp),
            ):
                yield
        finally:
            self.file_path = file_path

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


def run_script(
    test: Test,
    inputs: Iterable[str] = (),
    *,
    section: str | None = None,
    trace: Trace | None = None,
) -> str:
    """
    The console output of running the file ``test.file_path`` as a script, as a
    ``PrintTest`` runs it: until it ends or exits, with its ``input()`` calls answered
    with ``inputs``, and only the output of the calling thread captured. Of a
    notebook, the output of its code cells, or with a ``section``, only of that
    section's cells (a missing section raises before the notebook is run).

    With ``trace``, the script is traced as a whole.
    """
    with capture_stdout() as buffer, provide_input([str(i) for i in inputs]):
        if cells.is_notebook(test.file_path):
            if section is not None:
                # a missing section fails the test before the notebook is run
                sections.get_section(cells.read(test.file_path), section)
            # each cell's output is captured on its own
            run = loader.run_notebook(test.file_path, as_main=True, trace=trace)
            buffer.write(run.output(section=section))
        else:
            try:
                test.load_solution(as_main=True, trace=trace)
            except SystemExit:
                pass  # the program ended
    return buffer.getvalue()


class PrintTest(Test):
    """
    Console output of a file must match the desired output.

    The file is run as a script (with ``__name__ == "__main__"``) until it ends or exits,
    e.g. by calling ``sys.exit()``. Its ``input()`` calls are answered with ``inputs``,
    in order, which show up in the output like answers typed into a console. Whitespace
    that isn't visible in a console is ignored. Only the output of the thread running the
    test is captured, so that other threads (e.g. the CLI's) can keep writing to the
    console in the meantime.

    With ``isolated``, the file is run in a temporary working directory of its own,
    which is deleted afterwards (see ``Test._isolation``).

    A notebook's code cells are run instead, and their output is compared, which
    includes what Jupyter shows as text: the ``repr`` of objects passed to ``display``,
    and of each cell's final expression (unless it is ``None``, or the cell ends with
    ``;``). It only has to contain the desired output, so that what students output
    besides (e.g. the value of an expression they left at the end of a cell) doesn't
    fail the test: ``[1, 2]`` is also found in ``[[1, 2]]``. If no output is desired,
    there must be none. With a ``section`` (see ``Test.resolved_section``), the whole
    notebook is run, but only the output of the section's cells is compared. A test
    that isn't given a ``section`` inherits its task's; None compares the output of
    all cells.

    While a ``LIVE_SOURCE`` is set, it is skipped.
    """

    expected: str  # printout is necessarily str
    inputs: list[str]
    supports_trace = True
    # whether the test may be traced, e.g. by students who want to see what their code
    # does. Also the default for tests stored before it was introduced.
    traceable: bool = True
    # whether the test runs in a temporary working directory of its own (see
    # ``Test._isolation``). Only stored if set.
    isolated: bool = False
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
        isolated: bool = False,
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
        if isolated:
            self.isolated = True

    def run(self, trace: Trace | None = None) -> None:
        _skip_live_run()
        trace = trace if self.traceable else None
        is_notebook = cells.is_notebook(self.file_path)
        with self._isolation():
            output = run_script(
                self, self.inputs, section=self.resolved_section(), trace=trace
            )
        res, expected = _visible(output), _visible(self.expected)
        # a notebook's output only has to contain the expected one, unless there is
        # none, which any output would contain
        contains = is_notebook and expected != ""

        if not (expected in res if contains else res == expected):
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
            if contains:
                # the summary of what was compared, above the diff
                expl[0] = compare.summary(expected, res, op="in")

            raise AssertionError("\n".join(expl))


class Raises:
    """
    The ``expected`` value of a call of a ``SimpleTest`` that must raise an exception
    (or an exception of a subclass of it), e.g. ``Raises(ValueError)``. Given as the
    class or its name, which is stored. While the test runs, the name is looked up in
    the exercise file first, so that it can name exceptions the students define (also
    dotted, e.g. ``Raises("json.JSONDecodeError")`` if the file imports ``json``), and
    then among the builtins.
    """

    exception: str

    def __init__(self, exception: type[BaseException] | str):
        if isinstance(exception, type):
            if not issubclass(exception, BaseException):
                raise TypeError(f"{exception.__name__} is not an exception class")
            exception = exception.__name__
        if not isinstance(exception, str) or not all(
            part.isidentifier() for part in exception.split(".")
        ):
            raise TypeError(
                f"expected an exception class or its name, got {exception!r}"
            )
        self.exception = exception

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Raises) and other.exception == self.exception

    def __hash__(self) -> int:
        return hash((Raises, self.exception))

    def __repr__(self) -> str:
        return f"Raises({self.exception!r})"

    def resolve(self, module: ModuleType) -> type[BaseException]:
        """
        The exception class, looked up in ``module`` (the exercise file), then among
        the builtins.

        Raises
        ------
        AssertionError
            If neither has an exception class of this name.
        """
        try:
            cls = _resolve(module, self.exception)
        except AttributeError:
            cls = getattr(builtins, self.exception, None)
        if not (isinstance(cls, type) and issubclass(cls, BaseException)):
            raise AssertionError(
                f"{self.exception} is not an exception class: the exercise file "
                "doesn't define it, and it isn't a builtin exception"
            )
        return cls


class Make:
    """
    A value of a ``SimpleTest`` (an argument, keyword argument, constructor argument or
    expected value) that is created while the test runs, by calling ``name``, a class or
    function of the exercise file (maybe dotted), with ``args`` and ``kwargs``, e.g.
    ``Make("Graph", edges)``. So that the test uses the students' class, and only the
    name and the arguments are stored. The arguments can be ``Make`` values, too.
    """

    name: str
    args: list[Any]
    kwargs: dict[str, Any]

    def __init__(self, name: str, /, *args: Any, **kwargs: Any):
        if not isinstance(name, str) or not all(
            part.isidentifier() for part in name.split(".")
        ):
            raise TypeError(f"expected the name of a class or function, got {name!r}")
        self.name = name
        self.args = list(args)
        self.kwargs = kwargs

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Make) and (other.name, other.args, other.kwargs) == (
            self.name,
            self.args,
            self.kwargs,
        )

    __hash__ = None  # type: ignore[assignment]  # its arguments may be unhashable

    def __repr__(self) -> str:
        parts = [repr(self.name), *map(repr, self.args)]
        parts += [f"{k}={v!r}" for k, v in self.kwargs.items()]
        return f"Make({', '.join(parts)})"

    def build(self, module: ModuleType) -> Any:
        """Call ``name`` of ``module`` (the exercise file) with the arguments, which are
        built first (see ``_build``)."""
        args = _build(self.args, module)
        kwargs = _build(self.kwargs, module)
        return _resolve(module, self.name)(*args, **kwargs)


def _build(value: Any, module: ModuleType) -> Any:
    """``value`` with each ``Make`` in it, also in lists, tuples and dicts, replaced by
    the value it builds from ``module``."""
    if isinstance(value, Make):
        return value.build(module)
    if isinstance(value, list):
        return [_build(v, module) for v in value]
    if isinstance(value, tuple) and not hasattr(value, "_fields"):
        return tuple(_build(v, module) for v in value)
    if isinstance(value, dict):
        return type(value)((k, _build(v, module)) for k, v in value.items())
    return value


def _contains_make(value: Any) -> bool:
    """Whether ``value`` is or holds a ``Make``, also in lists, tuples and dicts."""
    if isinstance(value, Make):
        return True
    if isinstance(value, (list, tuple)):
        return any(_contains_make(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_make(v) for v in value.values())
    return False


class Call(NamedTuple):
    """One call of a ``SimpleTest`` (see ``SimpleTest.calls``)."""

    # the name of the function, method or attribute, maybe dotted
    name: str
    args: list[Any]
    kwargs: dict[str, Any]
    expected: Any


def _resolve(obj: Any, name: str) -> Any:
    """The attribute ``name`` of ``obj``, which may be dotted, e.g. ``a.b`` for
    ``obj.a.b``."""
    return functools.reduce(getattr, name.split("."), obj)


_REPR = reprlib.Repr(maxstring=40, maxother=40, maxlist=8, maxtuple=8, maxdict=8)


def _call_text(name: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    """A call of ``name`` as code, e.g. ``f(1, b=2)``, with long arguments shortened."""
    parts = [_REPR.repr(a) for a in args]
    parts += [f"{k}={_REPR.repr(v)}" for k, v in kwargs.items()]
    return f"{name}({', '.join(parts)})"


def _exception_text(err: BaseException) -> str:
    """``err`` as a line, e.g. ``KeyError: 'x'``."""
    return f"{type(err).__name__}: {err}" if str(err) else type(err).__name__


def _same_call(a: Call, b: Call) -> bool:
    """Whether ``a`` and ``b`` call the same name with the same arguments, expecting
    the same value."""
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return False


class SimpleTest(Test):
    """
    Output of a function must match the desired output for some parameters.

    The output is compared with ``compare.equal``: by ``==``, except for numpy arrays and
    pandas objects, which are compared as a whole.

    ``function_name``, ``function_args``, ``function_kwargs`` and ``expected`` can also
    be lists, with an item per call, for a sequence of calls on the same module (or
    object), which runs until the first call that fails (see ``calls``). With a
    ``constructor``, a class or function of the file that is called once per run, the
    names are looked up on the object it returns instead of the module. Names can be
    dotted (e.g. ``Stack.from_list``), and a name that isn't callable (e.g. a property)
    has its value compared. An ``expected`` value of ``Raises(...)`` expects the call to
    raise that exception. Arguments and expected values can be (or hold) ``Make(...)``
    values, which create an object of the exercise file while the test runs.

    With ``isolated``, the test runs in a temporary working directory of its own, which
    is deleted afterwards (see ``Test._isolation``).
    """

    # a str for a single call, a list for a sequence of calls (see ``calls``)
    function_name: str | list[str]
    # for a sequence, a list with the arguments of each call
    function_args: list[Any]
    # for a sequence, a list with the keyword arguments of each call
    function_kwargs: dict[str, Any] | list[dict[str, Any]]
    # for a sequence, a list with the expected value of each call
    expected: Any
    supports_trace = True
    # whether the test may be traced, which shows its arguments, too. Also the default
    # for tests stored before it was introduced.
    traceable: bool = True
    # the class or function of the file that creates the object whose methods are
    # called, and its arguments. Only stored if set, like ``isolated``, so that older
    # versions of pyics read tests without them as they are.
    constructor: str | None = None
    constructor_args: list[Any] = []
    constructor_kwargs: dict[str, Any] = {}
    # whether the test runs in a temporary working directory of its own (see
    # ``Test._isolation``)
    isolated: bool = False

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        function_name: str | list[str],
        expected: Any,
        *,
        function_args: list[Any] = [],
        function_kwargs: dict[str, Any] | list[dict[str, Any]] = {},
        constructor: str | None = None,
        constructor_args: list[Any] = [],
        constructor_kwargs: dict[str, Any] = {},
        traceable: bool = True,
        isolated: bool = False,
        **kwargs,
    ):
        """
        Raises
        ------
        ValueError
            If the lists of a sequence of calls don't have an item per call, or a name
            isn't a str.
        """
        self.name = name
        self.file_path = file_path
        if isinstance(function_name, str):
            self.function_name = function_name
            self.expected = expected
            self.function_args = function_args
            self.function_kwargs = function_kwargs
        else:
            (
                self.function_name,
                self.function_args,
                self.function_kwargs,
                self.expected,
            ) = _sequence(function_name, function_args, function_kwargs, expected)
        self.traceable = traceable
        if constructor is not None:
            if not isinstance(constructor, str):
                raise ValueError(
                    f"constructor must be a name, got {_REPR.repr(constructor)}"
                )
            self.constructor = constructor
            if not isinstance(constructor_args, (list, tuple)):
                raise ValueError("constructor_args must be a list of arguments")
            if not isinstance(constructor_kwargs, Mapping):
                raise ValueError("constructor_kwargs must be a dict of arguments")
            if constructor_args:
                self.constructor_args = list(constructor_args)
            if constructor_kwargs:
                self.constructor_kwargs = dict(constructor_kwargs)
        elif constructor_args or constructor_kwargs:
            raise ValueError("constructor arguments need a constructor")
        if isolated:
            self.isolated = True

    def calls(self) -> list[Call]:
        """The calls the test makes, in order: one, or a sequence (see
        ``SimpleTest``)."""
        if isinstance(self.function_name, str):
            return [
                Call(
                    self.function_name,
                    self.function_args,
                    self.function_kwargs,
                    self.expected,
                )
            ]
        return [
            Call(*call)
            for call in zip(
                self.function_name,
                self.function_args,
                self.function_kwargs,
                self.expected,
                strict=True,
            )
        ]

    def describe_calls(self) -> list[str]:
        """The calls the test makes as lines of code, e.g. ``.pop() -> 1``, after the
        call of its ``constructor``, if it has one, e.g. ``Stack()``."""
        lines, prefix = [], ""
        if self.constructor is not None:
            args, kwargs = self.constructor_args, self.constructor_kwargs
            lines.append(_call_text(self.constructor, args, kwargs))
            prefix = "."
        for call in self.calls():
            text = prefix + _call_text(call.name, call.args, call.kwargs)
            lines.append(f"{text} -> {_REPR.repr(call.expected)}")
        return lines

    def run(self, trace: Trace | None = None) -> None:
        trace = trace if self.traceable else None
        with self._isolation():
            self._run(trace)

    def _run(self, trace: Trace | None) -> None:
        def traced[T](call: Callable[[], T]) -> T:
            return call() if trace is None else trace.run(call)

        module = self.load_solution(trace=trace)
        calls = self.calls()
        # a single call of a module's function lets its errors propagate, as before
        # sequences and objects were introduced
        single = len(calls) == 1 and self.constructor is None
        target, prefix = module, ""
        # the calls so far, shown above the one that fails
        transcript: list[str] = []
        if (constructor := self.constructor) is not None:
            args = copy.deepcopy(self.constructor_args)
            kwargs = copy.deepcopy(self.constructor_kwargs)
            text = _call_text(constructor, args, kwargs)
            def construct() -> Any:
                built_args, built_kwargs = _build(args, module), _build(kwargs, module)
                return _resolve(module, constructor)(*built_args, **built_kwargs)

            try:
                target = traced(construct)
            except Exception as err:
                raise AssertionError(
                    f"creating the object, {text} raised {_exception_text(err)}"
                ) from err
            transcript.append(text)
            prefix = "."

        for i, call in enumerate(calls):
            # copied, so a solution that modifies its arguments can't change them for
            # the next call, or the next run of this test
            args = copy.deepcopy(call.args)
            kwargs = copy.deepcopy(call.kwargs)
            text = prefix + _call_text(call.name, args, kwargs)
            expected = call.expected
            # built before the call, so that a ``Raises`` can't match their errors
            if _contains_make([args, kwargs, expected]):
                try:
                    values = (args, kwargs, expected)
                    args, kwargs, expected = traced(
                        functools.partial(_build, values, module)
                    )
                except Exception as err:
                    line = f"creating the values of {text} raised {_exception_text(err)}"
                    raise AssertionError("\n".join([*transcript, line])) from err

            # these use the variables of this iteration, which they are only called in
            def invoke() -> Any:
                nonlocal text
                value = _resolve(target, call.name)
                if callable(value):
                    return value(*args, **kwargs)
                if args or kwargs:
                    raise TypeError(f"{prefix}{call.name} is not callable")
                text = prefix + call.name  # an attribute, whose value is compared
                return value

            def fail(line: str, explanation: list[str] | None = None) -> NoReturn:
                if single:
                    lines = explanation or [line]
                else:
                    lines = [*transcript, line, *(explanation or [])]
                # the calls before this one all returned their expected values
                if any(_same_call(call, earlier) for earlier in calls[:i]):
                    lines += ["", _STATE_HINT]
                raise AssertionError("\n".join(lines))

            if isinstance(call.expected, Raises):
                raises = call.expected.exception
                expected_cls = call.expected.resolve(module)
                try:
                    res = traced(invoke)
                except BaseException as err:
                    if isinstance(err, expected_cls):
                        transcript.append(f"{text} raised {type(err).__name__}")
                        continue
                    if not isinstance(err, Exception):
                        raise  # e.g. KeyboardInterrupt
                    fail(
                        f"expected {text} to raise {raises}, but it raised "
                        f"{_exception_text(err)}"
                    )
                fail(
                    f"expected {text} to raise {raises}, but it returned "
                    f"{_REPR.repr(res)}"
                )

            if single:
                res = traced(invoke)
            else:
                try:
                    res = traced(invoke)
                except Exception as err:
                    line = (
                        f"call {i + 1} of {len(calls)} ({text}) raised "
                        f"{_exception_text(err)}"
                    )
                    raise AssertionError("\n".join([*transcript, line])) from err
            if not compare.equal(res, expected):
                fail(
                    f"{text} returned {_REPR.repr(res)}, expected "
                    f"{_REPR.repr(call.expected)}",
                    compare.explain(res, expected),
                )
            transcript.append(f"{text} -> {_REPR.repr(res)}")


_STATE_HINT = (
    "An earlier identical call returned the expected value: the function keeps state "
    "between calls, e.g. in a mutable default argument or a global variable."
)


def _per_call(
    value: Any, label: str, n: int, kind: type | tuple[type, ...] | None = None
) -> list[Any]:
    """``value``, which must be a list with an item (of type ``kind``, the first of
    which names it) for each of the ``n`` calls of a sequence, as a list."""
    if not isinstance(value, (list, tuple)) or len(value) != n:
        raise ValueError(
            f"{label} must be a list with an item for each of the {n} calls, got "
            f"{_REPR.repr(value)}"
        )
    if kind is not None and not all(isinstance(v, kind) for v in value):
        name = (kind[0] if isinstance(kind, tuple) else kind).__name__
        raise ValueError(f"each item of {label} must be a {name}")
    return list(value)


def _sequence(
    names: Any, args: Any, kwargs: Any, expected: Any
) -> tuple[list[str], list[list[Any]], list[dict[str, Any]], list[Any]]:
    """
    The lists of a sequence of calls (see ``SimpleTest``), with no arguments for every
    call if ``args`` or ``kwargs`` are empty.

    Raises
    ------
    ValueError
        If a list doesn't have an item per call, or a name isn't a str.
    """
    if not isinstance(names, (list, tuple)) or not names:
        raise ValueError("function_name must be a name, or a non-empty list of names")
    for name in names:
        if not isinstance(name, str):
            raise ValueError(f"function names must be str, got {_REPR.repr(name)}")
    n = len(names)
    if isinstance(args, (list, tuple)) and not args:
        args = [[] for _ in names]
    if isinstance(kwargs, (list, tuple, Mapping)) and not kwargs:
        kwargs = [{} for _ in names]
    args = [list(a) for a in _per_call(args, "function_args", n, (list, tuple))]
    return (
        list(names),
        args,
        _per_call(kwargs, "function_kwargs", n, dict),
        _per_call(expected, "expected", n),
    )


def check_source(source: str) -> str:
    """
    Return ``source``, the pytest code of a ``GeneralTest``, if it can be parsed and
    defines a test: a top-level ``test*`` function or ``Test*`` class, as pytest
    collects them. Raise otherwise.

    Raises
    ------
    SyntaxError
        If ``source`` can't be parsed.
    ValueError
        If it defines no tests.
    """
    if not isinstance(source, str):
        raise TypeError(f"the source must be str, got {type(source).__name__}")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                return source
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return source
    raise ValueError(
        "the source defines no tests: a function named test... or a class named Test..."
    )


class GeneralTest(Test):
    """
    Tests written as pytest code, for what the other tests can't express.

    ``source`` is the code of a pytest test file, whose tests are collected by pytest
    itself, so that fixtures (also ones that ``yield``), ``parametrize``, classes,
    ``pytest.raises`` and ``pytest.approx`` work as usual. Each test it collects is a
    result of its own. Besides pytest's fixtures, it can use those of
    ``pyics.tests.general``, e.g. ``solution``, the exercise file ``file_path`` loaded
    as a module.

    With ``isolated``, each test runs in a temporary working directory of its own,
    which is deleted afterwards (see ``Test._isolation``).
    """

    source: str
    supports_trace = True
    # whether the test may be traced, e.g. by students who want to see what their code
    # does
    traceable: bool = True
    # whether the test runs in a temporary working directory of its own (see
    # ``Test._isolation``). Only stored if set.
    isolated: bool = False

    def __init__(
        self,
        name: str,
        file_path: StrPath,
        source: str,
        *,
        traceable: bool = True,
        isolated: bool = False,
        **kwargs,
    ):
        """
        Raises
        ------
        SyntaxError
            If ``source`` can't be parsed.
        ValueError
            If it defines no tests (see ``check_source``).
        """
        self.name = name
        self.file_path = file_path
        self.source = check_source(source)
        self.traceable = traceable
        if isolated:
            self.isolated = True

    def run(self, trace: Trace | None = None) -> None:
        """
        Run the test's tests through pytest (see ``pytest_integration.run_tests``),
        raising an ``AssertionError`` that lists those that didn't pass. While a
        task's tests run, its tests are collected as items of their own instead, so
        that this isn't used there.
        """
        # imported here, since pytest_integration imports this module
        from pyics.tests.pytest_integration import run_tests

        results = run_tests([self], tracing=trace is not None)
        failed = [r for r in results if r.when == "call" and r.outcome != "passed"]
        if failed:
            raise AssertionError(
                "\n".join(
                    f"{r.nodeid} {r.outcome}: {r.message_text.plain}" for r in failed
                )
            )


class SpecTest(Test):
    """
    Symbols in file match some specification.

    ``expected`` is either a spec dict (see ``check_specs``), or a class or function,
    which is converted into a spec dict so that the test can be stored as json. If
    ``by_type`` is set, parameters and attributes are matched by their type only, ignoring
    their names and order. ``symbol_name`` can be dotted, e.g. ``Stack.push`` for a method.
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
        try:
            symb = _resolve(m, self.symbol_name)
        except AttributeError as err:
            raise AssertionError(
                f"{Path(self.file_path).name} has no symbol {self.symbol_name!r}"
            ) from err

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
    can't be parsed (they are named if the test fails).

    While a ``LIVE_SOURCE`` is set, it is skipped.
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
        _skip_live_run()
        path = Path(self.file_path)
        if cells.is_notebook(path):
            section = self.resolved_section()
            source = loader.python_source(path, section=section)
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
