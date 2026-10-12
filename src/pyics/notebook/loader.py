"""
Run the code cells of a notebook as a module, like a fresh run of the saved notebook in
a new kernel, and turn them into Python source, e.g. for ``StructureTest``.

Cells go through IPython's ``TransformerManager`` first, so that magics (``%matplotlib
inline``) and shell commands (``x = !ls``) become calls of ``get_ipython()``. A stand-in
answers them (see ``_Shell``): magics and shell commands do nothing, except for
``%%time``, ``%%timeit`` and ``%%capture`` (and ``%time`` and ``%timeit``), which run
their code once. Other cell magics (``%%writefile``, ``%%bash``, ...) are skipped.

Each code cell is then compiled on its own, under the file name ``<path>:cell N`` (N
counts all cells from 1), and the code it runs is registered in ``linecache`` as the
file's source, so that tracebacks and traces show e.g. ``bfs.ipynb:cell 3`` with the
cell's lines, which keep their numbers. That is the cell's source, apart from magics
and shell commands, which show up as the calls of ``get_ipython()`` they run: traces
(``snoop``) only show the source of a file that can be parsed.

Errors don't stop a run: the error of each cell is recorded (see ``CellError``), and the
next cell still runs, so that an unfinished exercise doesn't break the others. Cells
tagged ``skip-execution`` (e.g. downloads or training) don't run.
"""

import ast
import contextlib
import functools
import io
import linecache
import os
import sys
import threading
import tokenize
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass, field
from types import CodeType, ModuleType
from typing import Any, NamedTuple

from pyics.notebook import cells, sections
from pyics.notebook.cells import Cell
from pyics.tests.capture import capture_stdout
from pyics.tests.trace import Trace
from pyics.util.paths import StrPath

# the tag of cells that aren't run, as nbclient uses it
SKIP_TAG = "skip-execution"
# cell magics whose body is Python, which is run once (without timing it, or showing
# the captured output). The bodies of other cell magics aren't Python, and are skipped.
_CODE_CELL_MAGICS = frozenset({"time", "timeit", "capture"})
# line magics whose statement is run once
_CODE_LINE_MAGICS = frozenset({"time", "timeit"})
# the module variable holding the value of a cell's final expression while it runs. Its
# dunder name keeps it out of traces (see ``trace._FrameInfo``).
_OUT = "__pyics_out__"

_local = threading.local()


def last_errors() -> list[CellError]:
    """The errors of the cells of the notebook that was run last in this thread (see
    ``run_notebook``), until they are cleared (see ``clear_last_errors``)."""
    return list(getattr(_local, "errors", ()))


def clear_last_errors() -> None:
    """Forget the errors of the notebook that was run last in this thread, e.g. before
    a test that may run one."""
    _local.errors = []


@dataclass(frozen=True)
class CellError:
    """The error a cell raised when a notebook was run."""

    cell: Cell
    error: BaseException

    def describe(self) -> str:
        """The error, e.g. "cell 7 raised NameError: name 'qeue' is not defined"."""
        err = self.error
        if isinstance(err, SyntaxError):
            # its message would name the file, and the line in a form of its own
            what = err.msg + (f" (line {err.lineno})" if err.lineno else "")
        else:
            what = str(err)
        text = f"cell {self.cell.number} raised {type(err).__name__}"
        return f"{text}: {what}" if what else text


@dataclass
class NotebookRun:
    """A run of a notebook's code cells (see ``run_notebook``)."""

    # the module the cells ran in
    module: ModuleType
    # the errors of the cells that raised one, in order
    errors: list[CellError] = field(default_factory=list)
    # what each cell that ran wrote to stdout, by the cell's index: printed text, and
    # what Jupyter shows as text, i.e. the ``repr`` of objects passed to ``display``,
    # and of the cell's final expression
    outputs: dict[int, str] = field(default_factory=dict)
    # the notebook that was run, as read by ``cells.read``
    notebook: dict[str, Any] | None = None

    def output(
        self, of: Iterable[Cell] | None = None, *, section: str | None = None
    ) -> str:
        """
        The output of the cells ``of``, or of the cells of the notebook's section
        ``section`` (see ``pyics.notebook.sections``), or of all cells by default, in
        their order in the notebook.

        Raises
        ------
        SectionNotFound
            If the notebook that was run has no section ``section``.
        """
        if section is not None:
            if of is not None:
                raise TypeError("give either the cells or the section, not both")
            if self.notebook is None:
                raise ValueError("the notebook that was run isn't known")
            of = sections.get_section(self.notebook, section).cells
        if of is None:
            return "".join(self.outputs[i] for i in sorted(self.outputs))
        indices = sorted({cell.index for cell in of})
        return "".join(self.outputs.get(i, "") for i in indices)


def cell_filename(path: StrPath, cell: Cell) -> str:
    """The file name the code of ``cell`` of the notebook at ``path`` is compiled
    under, e.g. ``.exercises/bfs.ipynb:cell 3``."""
    return f"{os.fspath(path)}:cell {cell.number}"


# --- turning cells into Python ---


@functools.cache
def _transformer() -> Any:
    # imported when first needed, since importing IPython takes a while
    from IPython.core.inputtransformer2 import TransformerManager

    return TransformerManager()


def transform(source: str) -> str:
    """
    The cell source ``source`` with IPython's syntax (magics, shell commands, ``x?``)
    turned into Python, as IPython runs it. Lines keep their numbers, apart from lines
    joined by a magic that is continued with ``\\``.
    """
    lines = source.splitlines(keepends=True)
    blank = 0
    while blank < len(lines) and not lines[blank].strip():
        blank += 1
    rest = "".join(lines[blank:])
    try:
        code = _transformer().transform_cell(rest) if rest else ""
    except Exception:  # noqa: BLE001
        code = rest  # e.g. a tokenizing error, which compiling it reports
    # IPython removes leading blank lines, which would shift the lines that follow
    return "\n" * blank + code


class _CellMagic(NamedTuple):
    """The cell magic a cell starts with, e.g. ``%%capture out``."""

    # e.g. "capture"
    name: str
    # what follows its name on its line, e.g. "out"
    args: str
    # the cell's lines after it
    body: str
    # the line of the cell it is on, from 1
    line: int


def _cell_magic(source: str) -> _CellMagic | None:
    """The cell magic the cell source ``source`` starts with, if it does."""
    lines = source.splitlines(keepends=True)
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or not lines[first].startswith("%%"):
        return None
    name, _, args = lines[first][2:].strip().partition(" ")
    return _CellMagic(name, args.strip(), "".join(lines[first + 1 :]), first + 1)


def _python_code(source: str) -> str | None:
    """
    The cell source ``source`` as the Python it runs, with its lines at their numbers in
    the cell (see ``transform``). For a cell magic that runs its body (e.g. ``%%time``),
    that is its body, below the magic as a comment. None for a cell magic whose body
    isn't Python (e.g. ``%%bash``), which isn't run.
    """
    if (magic := _cell_magic(source)) is None:
        return transform(source)
    if magic.name not in _CODE_CELL_MAGICS:
        return None
    above = "\n" * (magic.line - 1) + f"# %%{magic.name} {magic.args}".rstrip() + "\n"
    return above + transform(magic.body)


@dataclass(frozen=True)
class NotebookSource:
    """The code cells of a notebook as Python source (see ``python_source``)."""

    # the code of the cells, one after the other
    source: str
    # the cells whose code is in ``source``, in order
    cells: list[Cell]
    # the cells left out since their code can't be parsed, with the error
    unparsable: list[tuple[Cell, SyntaxError]]
    # the line of ``source`` the code of each of ``cells`` starts at, from 1
    starts: list[int] = field(default_factory=list)

    def locate(self, line: int) -> str:
        """Where the line ``line`` of ``source`` (from 1) is in the notebook, e.g.
        "cell 3, line 2"."""
        for cell, start in zip(reversed(self.cells), reversed(self.starts)):
            if start <= line:
                return f"cell {cell.number}, line {line - start + 1}"
        return f"line {line}"


def _source_of(code_cells: Iterable[Cell]) -> NotebookSource:
    """``code_cells`` as Python source (see ``python_source``)."""
    parts: list[str] = []
    included: list[Cell] = []
    unparsable: list[tuple[Cell, SyntaxError]] = []
    starts: list[int] = []
    line = 1
    for cell in code_cells:
        code = _python_code(cell.source)
        if code is None:
            continue
        try:
            ast.parse(code)
        except SyntaxError as err:
            unparsable.append((cell, err))
            continue
        # every cell takes at least a line, so that each line is in one cell
        code = code if code.endswith("\n") else code + "\n"
        parts.append(code)
        included.append(cell)
        starts.append(line)
        line += code.count("\n")
    return NotebookSource("".join(parts), included, unparsable, starts)


def python_source(
    path_or_nb: StrPath | dict[str, Any], *, section: str | None = None
) -> NotebookSource:
    """
    The code cells of the notebook at ``path_or_nb`` (or of the notebook itself, as read
    by ``cells.parse``) as Python source, e.g. to check its structure: their code one
    after the other, with magics and shell commands turned into Python (see
    ``transform``). The bodies of cell magics that run them (``%%time``,
    ``%%timeit`` and ``%%capture``) are included, the cells of other cell magics (e.g.
    ``%%bash``) and those that can't be parsed are left out. Cells tagged
    ``skip-execution`` are included, as they are part of the notebook's code.

    With ``section``, only the code cells of the notebook's section of that name (see
    ``pyics.notebook.sections``) are included.

    Raises
    ------
    NotebookError
        If the notebook isn't a notebook in nbformat 4.
    SectionNotFound
        If the notebook has no section ``section``.
    """
    nb = path_or_nb if isinstance(path_or_nb, dict) else cells.read(path_or_nb)
    if section is None:
        return _source_of(cells.code_cells(nb))
    return _source_of(sections.get_section(nb, section).code_cells)


# --- running cells ---


def _ends_with_semicolon(code: str) -> bool:
    """Whether the last token of ``code``, apart from comments, is ``;``, which hides
    the value of a cell's final expression in Jupyter."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except tokenize.TokenError, SyntaxError:
        return False
    ignored = {
        tokenize.ENDMARKER,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.COMMENT,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
    last = next((t for t in reversed(tokens) if t.type not in ignored), None)
    return last is not None and last.type == tokenize.OP and last.string == ";"


def _compile(code: str, filename: str) -> CodeType:
    """
    Compile the cell code ``code`` under ``filename``. The value of its final
    expression, which Jupyter shows (as ``Out[n]``), is assigned to ``_OUT``, unless the
    code ends with ``;``.

    Raises
    ------
    SyntaxError
        If ``code`` can't be compiled.
    """
    tree = ast.parse(code, filename)
    last = tree.body[-1] if tree.body else None
    if isinstance(last, ast.Expr) and not _ends_with_semicolon(code):
        target = ast.Name(_OUT, ast.Store())
        assign = ast.Assign(targets=[target], value=last.value)
        tree.body[-1] = ast.fix_missing_locations(ast.copy_location(assign, last))
    return compile(tree, filename, "exec", dont_inherit=True)


def _register_source(filename: str, source: str) -> None:
    """Register ``source`` as the contents of the file ``filename`` in ``linecache``,
    where tracebacks, ``inspect`` and traces look up source lines. Without a
    modification time, ``linecache.checkcache`` keeps it."""
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    linecache.cache[filename] = (len(source), None, lines, filename)


def _display(*objs: object, **kwargs: object) -> None:
    """Stands in for IPython's ``display``: shows each object as its ``repr``, as
    Jupyter shows objects as text."""
    for obj in objs:
        print(repr(obj))


class _CapturedIO:
    """What ``%%capture <name>`` assigns to ``<name>``: the output of the cell."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.outputs: list[object] = []

    def show(self) -> None:
        print(self.stdout, end="")

    __call__ = show


class _Shell:
    """
    Stands in for IPython's shell, which the code of cells calls as ``get_ipython()``
    for magics and shell commands (see ``transform``): they do nothing, and shell
    commands have no output. The magics ``%%time``, ``%%timeit`` and ``%%capture`` (and
    ``%time`` and ``%timeit``) run their code once instead, without timing it or
    showing the captured output.
    """

    def __init__(self, namespace: dict[str, Any], trace: Trace | None):
        self.user_ns = namespace
        self._trace = trace

    def execute(self, code: CodeType) -> Any:
        """Run ``code`` (see ``_compile``), and return the value of its final
        expression. With a trace, ``code`` is watched by it."""
        if self._trace is not None:
            self._trace.watch(code)
        try:
            exec(code, self.user_ns)  # noqa: S102
            return self.user_ns.get(_OUT)
        finally:
            self.user_ns.pop(_OUT, None)

    def run_magic_body(self, name: str, args: str, run: Callable[[], Any]) -> Any:
        """Run the body of the cell magic ``%%<name> <args>`` with ``run``, which
        returns the value of its final expression, if the magic runs its body, and
        return that value as the magic's."""
        if name not in _CODE_CELL_MAGICS:
            return None
        if name != "capture":
            return run()
        with capture_stdout() as buffer:
            if (value := run()) is not None:
                print(repr(value))
        if names := [arg for arg in args.split() if not arg.startswith("-")]:
            self.user_ns[names[0]] = _CapturedIO(buffer.getvalue())
        return None

    def run_line_magic(self, magic_name: str, line: str, _stack_depth: int = 1) -> Any:
        if magic_name not in _CODE_LINE_MAGICS:
            return None
        # compiled at the line of the magic in the code that calls it, i.e. the cell
        caller = sys._getframe(1)
        code = "\n" * (caller.f_lineno - 1) + transform(line)
        return self.execute(_compile(code, caller.f_code.co_filename))

    def run_cell_magic(self, magic_name: str, line: str, cell: str) -> Any:
        # compiled at the lines below the magic in the code that calls it
        caller = sys._getframe(1)
        code = "\n" * caller.f_lineno + transform(cell)
        filename = caller.f_code.co_filename
        return self.run_magic_body(
            magic_name, line, lambda: self.execute(_compile(code, filename))
        )

    def magic(self, line: str) -> None:
        return None

    def system(self, cmd: str) -> None:
        return None

    def getoutput(self, cmd: str, split: bool = True, depth: int = 0) -> Any:
        from IPython.utils.text import SList

        return SList() if split else ""


@contextlib.contextmanager
def _non_interactive_plots() -> Generator[None]:
    """
    Draw plots with matplotlib's non-interactive Agg backend, so that e.g.
    ``plt.show()`` doesn't open a window or block. Figures are closed afterwards, as
    Jupyter closes them once they are shown.
    """
    previous = os.environ.get("MPLBACKEND")
    os.environ["MPLBACKEND"] = "Agg"
    if (matplotlib := sys.modules.get("matplotlib")) is not None:
        # if this fails, plots may block, but the cells still run
        with contextlib.suppress(Exception):
            matplotlib.use("Agg")  # also switches pyplot's backend, if it is imported
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MPLBACKEND", None)
        else:
            os.environ["MPLBACKEND"] = previous
        if (pyplot := sys.modules.get("matplotlib.pyplot")) is not None:
            with contextlib.suppress(Exception):
                pyplot.close("all")


class _Runner:
    """Runs the code cells of the notebook at ``path`` in ``run.module``."""

    def __init__(self, path: StrPath, run: NotebookRun, trace: Trace | None):
        self.path = path
        self.run = run
        namespace = run.module.__dict__
        self.shell = _Shell(namespace, trace)
        namespace["get_ipython"] = lambda: self.shell
        namespace["display"] = _display

    def run_cells(self, code_cells: Iterable[Cell]) -> None:
        for cell in code_cells:
            if SKIP_TAG in cell.tags:
                continue
            exited = False
            # each cell's output on its own; it never reaches the console
            with capture_stdout() as buffer:
                try:
                    self._run_cell(cell)
                except SystemExit:
                    exited = True  # like a script that exits
                except Exception as err:  # noqa: BLE001
                    self.run.errors.append(CellError(cell, err))
            self.run.outputs[cell.index] = buffer.getvalue()
            if exited:
                return

    def _run_cell(self, cell: Cell) -> None:
        code = _python_code(cell.source)
        if code is None:
            return  # e.g. %%writefile, which is skipped
        filename = cell_filename(self.path, cell)
        # the code that runs, which traces can only show if it can be parsed.
        # Registered before compiling, so that syntax errors can show their line, too.
        _register_source(filename, code)
        compiled = _compile(code, filename)
        if (magic := _cell_magic(cell.source)) is None:
            value = self.shell.execute(compiled)
        else:
            run = functools.partial(self.shell.execute, compiled)
            value = self.shell.run_magic_body(magic.name, magic.args, run)
        if value is not None:
            print(repr(value))


def run_notebook(
    path: StrPath,
    *,
    as_main: bool = False,
    trace: Trace | None = None,
    module_name: str = "current_solution",
) -> NotebookRun:
    """
    Run the code cells of the notebook at ``path`` in order, in a new module, which is
    registered in ``sys.modules`` as ``module_name``. With ``as_main``, it is run as a
    script, i.e. with ``__name__ == "__main__"``.

    An error in a cell is recorded, and the next cell still runs. ``SystemExit`` ends
    the run, like a script that exits. Cells tagged ``skip-execution`` don't run. The
    module has ``display``, which prints the ``repr`` of objects, and ``get_ipython()``
    for magics (see ``transform``). Each cell's output is captured on its own (see
    ``NotebookRun.outputs``), so ``input()`` answers given by ``provide_input`` show up
    in the output of the cell that asks for them.

    With ``trace``, the cells' code is watched by it (see ``Trace.watch``), so that the
    calls into it that are run by ``trace`` are traced. A script is traced as a whole.

    The errors of the run are also available as ``last_errors()``.

    Raises
    ------
    NotebookError
        If the file isn't a notebook in nbformat 4.
    """
    nb = cells.read(path)
    module = ModuleType(module_name)
    if as_main:
        module.__name__ = "__main__"
    sys.modules[module_name] = module

    run = NotebookRun(module, notebook=nb)
    runner = _Runner(path, run, trace)
    code_cells = cells.code_cells(nb)
    try:
        with _non_interactive_plots():
            if trace is not None and as_main:
                trace.run(lambda: runner.run_cells(code_cells))
            else:
                runner.run_cells(code_cells)
    finally:
        _local.errors = list(run.errors)
    return run


def load_module(
    path: StrPath, *, as_main: bool = False, trace: Trace | None = None
) -> ModuleType:
    """The module the code cells of the notebook at ``path`` ran in, as the module
    ``current_solution`` (see ``run_notebook``)."""
    return run_notebook(path, as_main=as_main, trace=trace).module
