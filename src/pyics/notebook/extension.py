"""
The IPython extension that runs the tests of a notebook's tasks whenever one of its
cells runs ("live runs"), and passes their results to the TUI through the notebook's
channel (see ``pyics.notebook.channel``). The notebook itself never shows anything of
it, except a one-line warning if no task can be found for it.

The "Python (pyics)" kernel loads it at every start (``--ext=pyics``, see
``pyics.notebook.kernelspec``); any other IPython kernel loads it with ``%load_ext
pyics``.

**Finding the tasks.** The notebook's path comes from ``__session__`` (set by ipykernel
from ``JPY_SESSION_NAME``, which Jupyter's server sets) or ``__vsc_ipynb_file__`` (set
by VS Code once the kernel runs, so it is looked for again at each cell run). The
course manifests in the notebook's directory and in the ``PARENT_LEVELS`` directories
above it (and in their subdirectories) are searched for tasks whose exercise files
include the notebook (see ``find_tasks``). ``%pyics task <task file>`` names a task
instead; ``%pyics off`` and ``%pyics on`` pause and resume live runs.

**Live runs.** After each cell (IPython's ``post_run_cell`` event), the tests of the
tasks run synchronously in the kernel, with ``SOLUTION_SOURCE`` set to a ``LiveSource``:
``SimpleTest`` and ``SpecTest`` test the objects as the kernel currently defines them,
``StructureTest`` checks the cells as they were last run (see ``CellLog``), and
``PrintTest`` is skipped, as it needs a fresh run of the saved notebook. Their results
are written to the channel. The control file of the channel turns tracing on, or pauses
live runs.

**Silence.** Nothing a live run does reaches the notebook (see ``_isolated``): what the
tested code prints to ``sys.stdout`` and ``sys.stderr`` is captured, what it displays
(``display``, plots of matplotlib's inline backend) is dropped, the figures it opens are
closed, ``input()`` raises ``EOFError`` rather than asking the notebook, and the
extension's own logs go to the kernel's log (the terminal Jupyter runs in). Interrupting
the kernel stops a live run, e.g. an endless loop in a tested function; the test that
was running is reported as interrupted. Errors of the extension are logged, and never
break the kernel.

Not silenced: output written by compiled code straight to the process's file
descriptors (which ipykernel forwards to the notebook), widgets the tested code
creates (their comms still reach the frontend), and windows of GUI backends of
matplotlib (e.g. after ``%matplotlib qt``).
"""

import contextlib
import dataclasses
import inspect
import logging
import os
import signal
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import Any

import pytest
from rich.markup import escape

import pyics.util.json_handling as json
from pyics.data.task import Task
from pyics.distribution.dependencies import ModulePolicy, manifest_requirements
from pyics.notebook import cells, channel, loader, sections
from pyics.notebook.cells import Cell
from pyics.notebook.channel import CellRef, KernelChannel
from pyics.tests.capture import capture_stderr, capture_stdout, provide_input
from pyics.tests.pytest_integration import LiveTestCollectionPlugin
from pyics.tests.results import TestResult
from pyics.tests.tests import SOLUTION_SOURCE
from pyics.tests.trace import Trace
from pyics.util import paths
from pyics.util.discovery import find_courses
from pyics.util.paths import StrPath

# pytest's arguments for live runs. Besides those of the TUI's runs, they keep the
# student's and the course's pytest setup (conftest files, plugins, ``addopts``) out
# of the kernel, and leave out the plugins that change the process's state while
# pytest runs (faulthandler's file, ``sys.unraisablehook``, ``threading.excepthook``)
# and collect garbage after each run, which makes a run a few times faster.
PYTEST_ARGS = (
    "-p",
    "no:terminal",
    "-p",
    "no:cacheprovider",
    "--capture=no",
    "--noconftest",
    "--disable-plugin-autoload",
    "-o",
    "addopts=",
    "-p",
    "no:faulthandler",
    "-p",
    "no:unraisableexception",
    "-p",
    "no:threadexception",
)
# how many directories above the notebook's are searched for course manifests (each
# with its subdirectories)
PARENT_LEVELS = 3
# the seconds after which the tasks of a notebook none were found for are searched again
SEARCH_AGAIN_AFTER = 30.0
# the most cell runs remembered to place cells that aren't in the saved notebook
MAX_RUNS = 10_000
# the name the module of the kernel's objects is registered under while tests run, like
# the module of an exercise file (see ``Test.load_solution``)
MODULE_NAME = "current_solution"
# the warning shown (once) in the notebook if no task can be found for it
NO_TASK_WARNING = (
    "pyics: no task found for {name} (no course near it lists it as an exercise "
    "file), so no tests run when its cells run. Name its task with: "
    "%pyics task <path of its .pyicstask.json file>"
)
USAGE = "%pyics task <task file> | %pyics off | %pyics on"

_logger = logging.getLogger(__name__)
# the attributes of ``sys`` that pytest sets for post-mortem debugging when a test
# fails, which would make ``%debug`` debug the test rather than the student's error
_LAST_ERROR = ("last_type", "last_value", "last_traceback", "last_exc")


def _same(path: StrPath) -> str:
    """``path`` in a form that is the same for all paths to the same file."""
    return os.path.normcase(os.path.realpath(path))


# --- finding the notebook and its tasks ---


def notebook_path(namespace: Mapping[str, Any], cwd: StrPath) -> Path | None:
    """
    The path of the notebook a kernel runs, by the variables its frontend sets in the
    kernel's ``namespace``: ``__vsc_ipynb_file__`` (VS Code), or ``__session__``
    (ipykernel, from Jupyter's ``JPY_SESSION_NAME``). None if neither names a notebook.
    A relative path is looked up in the kernel's working directory ``cwd``, which is the
    notebook's directory (older Jupyter servers give the path relative to their root).
    """
    for name in ("__vsc_ipynb_file__", "__session__"):
        value = namespace.get(name)
        if not isinstance(value, str) or not cells.is_notebook(value.strip()):
            continue
        path = Path(value.strip()).expanduser()
        if path.is_absolute():
            return paths.absolute(path)
        for candidate in (Path(cwd) / path, Path(cwd) / path.name):
            if candidate.exists():
                return paths.absolute(candidate)
        return paths.absolute(Path(cwd) / path)
    return None


def _lists_notebook(task_file: Path, notebook: Path) -> bool:
    """Whether the task file ``task_file``, read as plain data (so that nothing is
    decoded), has ``notebook`` among its exercise files."""
    try:
        data = json.load_plain(task_file)
        ex_files = data.get("ex_files") or []
        wanted = _same(notebook)
        return any(
            _same(paths.resolve(f, task_file.parent)) == wanted for f in ex_files
        )
    except Exception:  # noqa: BLE001
        _logger.debug(f"Skipping the task file {task_file}", exc_info=True)
        return False


def _search_roots(directory: Path, levels: int) -> list[Path]:
    """``directory`` and the ``levels`` directories above it."""
    directory = paths.absolute(directory)
    return [directory, *list(directory.parents)[:levels]]


def find_tasks(
    notebook: StrPath, *, levels: int = PARENT_LEVELS
) -> dict[Path, list[str]]:
    """
    The task files whose exercise files include ``notebook``, of the courses whose
    manifests are in the notebook's directory or in one of the ``levels`` directories
    above it, or in one of their subdirectories (see ``find_courses``). Each is mapped
    to the ``requires`` of its course, which its objects may be decoded with (see
    ``ModulePolicy``). Task files are read as plain data, so that nothing is decoded.
    """
    notebook = paths.absolute(notebook)
    found: dict[Path, list[str]] = {}
    seen: set[str] = set()
    for root in _search_roots(notebook.parent, levels):
        for manifest_path, manifest in find_courses(root, max_depth=1).items():
            if _same(manifest_path) in seen:
                continue
            seen.add(_same(manifest_path))
            try:
                requires, _ = manifest_requirements(manifest)
            except ValueError:
                _logger.warning(f"Skipping the course {manifest_path}", exc_info=True)
                continue
            for task_file in manifest["tasks"].values():
                path = paths.resolve(task_file, manifest_path.parent)
                if path not in found and _lists_notebook(path, notebook):
                    found[path] = list(requires)
    return found


def _course_requires(task_file: Path) -> list[str] | None:
    """The ``requires`` of the course near ``task_file`` that includes it (searched for
    like by ``find_tasks``), None if no course does."""
    wanted = _same(task_file)
    for root in _search_roots(task_file.parent, PARENT_LEVELS):
        for manifest_path, manifest in find_courses(root, max_depth=1).items():
            base = manifest_path.parent
            if any(
                _same(paths.resolve(t, base)) == wanted
                for t in manifest["tasks"].values()
            ):
                try:
                    return manifest_requirements(manifest)[0]
                except ValueError:
                    continue
    return None


# --- the cells the kernel ran ---


class CellLog:
    """
    The code of the cells a kernel ran: the source each cell ran last (by the id its
    frontend gave it), and the order cells ran in, to place cells that aren't in the
    saved notebook (see ``code_cells``).

    A cell is matched with the saved notebook by its id. Frontends that give cells ids
    of their own (e.g. VS Code) are matched by source: a cell that ran the source of a
    saved cell is that cell from then on, even after it changes.
    """

    def __init__(self, max_runs: int = MAX_RUNS):
        # the latest source of each cell, by its id
        self.sources: dict[str, str] = {}
        # the ids of the cells that ran, in order (the latest ``max_runs`` runs)
        self._runs: deque[str] = deque(maxlen=max_runs)
        # the saved cell each cell of a frontend's own ids is, learned by its source
        self._aliases: dict[str, str] = {}

    def record(self, cell_id: str | None, source: str) -> None:
        """Remember that the cell ``cell_id`` ran ``source``. Cells without an id can't
        be matched with the saved notebook, and are left out."""
        if not cell_id:
            return
        self.sources[cell_id] = source
        self._runs.append(cell_id)

    def match(self, saved: list[Cell]) -> dict[str, Cell]:
        """The cell of the saved notebook's cells ``saved`` that each cell that ran is,
        by the id of the cell that ran. Cells that aren't in the notebook are left
        out."""
        code = {cell.id: cell for cell in saved if cell.cell_type == "code" and cell.id}
        matched = {key: code[key] for key in self.sources if key in code}
        taken = {cell.index for cell in matched.values()}
        for key, source in self.sources.items():
            if key in matched:
                continue
            cell = code.get(self._aliases.get(key, ""))
            if cell is None or cell.index in taken:
                cell = _same_source(source, saved, taken)
            if cell is not None:
                matched[key] = cell
                taken.add(cell.index)
                if cell.id:
                    self._aliases[key] = cell.id
        return matched

    def code_cells(self, nb: dict[str, Any], section: str | None = None) -> list[Cell]:
        """
        The code cells of the saved notebook ``nb`` (of its section ``section``, if
        given) with the source they ran last, in the notebook's order. Cells that ran,
        but aren't in the notebook (e.g. new cells that weren't saved yet), are placed
        right after the cell of the notebook that ran most recently before them (at the
        start if none did). They are numbered as if they were saved there, as their
        position isn't known.

        Raises
        ------
        SectionNotFound
            If the notebook has no section ``section``.
        """
        members = None
        if section is not None:
            members = {cell.index for cell in sections.get_section(nb, section).cells}
        saved = cells.cells(nb)
        matched = self.match(saved)
        source_of = {cell.index: self.sources[key] for key, cell in matched.items()}

        runs = list(self._runs)
        last_run = {key: i for i, key in enumerate(runs)}
        # the cells that aren't in the notebook, by the index of the cell they follow
        # (-1 for the start), in the order they last ran
        placed: dict[int, list[str]] = {}
        unknown = [key for key in self.sources if key not in matched]
        for key in sorted(unknown, key=lambda k: last_run.get(k, -1)):
            before = range(last_run.get(key, 0) - 1, -1, -1)
            after = next(
                (matched[runs[i]].index for i in before if runs[i] in matched), -1
            )
            placed.setdefault(after, []).append(key)

        def new_cells(after: int) -> list[Cell]:
            return [
                Cell(after + i, "code", key, (), self.sources[key])
                for i, key in enumerate(placed.get(after, ()), 1)
            ]

        result = new_cells(-1) if members is None else []
        for cell in saved:
            if members is not None and cell.index not in members:
                continue
            if cell.cell_type == "code":
                if (source := source_of.get(cell.index)) is not None:
                    cell = dataclasses.replace(cell, source=source)
                result.append(cell)
            result += new_cells(cell.index)
        return result


def _same_source(source: str, saved: list[Cell], taken: set[int]) -> Cell | None:
    """The first code cell of ``saved`` that isn't ``taken`` whose source is
    ``source`` (apart from whitespace at its end), if ``source`` isn't blank."""
    if not (wanted := source.rstrip()):
        return None
    return next(
        (
            cell
            for cell in saved
            if cell.cell_type == "code"
            and cell.index not in taken
            and cell.source.rstrip() == wanted
        ),
        None,
    )


class _SavedNotebook:
    """The saved notebook at ``path``, read again only when the file changed."""

    def __init__(self, path: Path):
        self.path = path
        self._key: tuple[int, int] | None = None
        self._nb: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        """
        The notebook (see ``cells.read``).

        Raises
        ------
        NotebookError
            If the file isn't a notebook in nbformat 4.
        OSError
            If the file can't be read.
        """
        st = os.stat(self.path)
        key = (st.st_mtime_ns, st.st_size)
        if self._nb is None or key != self._key:
            self._nb, self._key = cells.read(self.path), key
        return self._nb


# --- live runs ---


def _defined_code(namespace: Mapping[str, Any]) -> Iterator[CodeType]:
    """The code of the functions and classes (their methods and properties) defined in
    the kernel's main module, whose namespace is ``namespace``, e.g. to trace them."""
    module_name = namespace.get("__name__", "__main__")
    seen: set[int] = set()

    def code_of(obj: Any, depth: int) -> Iterator[CodeType]:
        if id(obj) in seen or depth > 3:
            return
        seen.add(id(obj))
        if isinstance(obj, (staticmethod, classmethod)):
            yield from code_of(obj.__func__, depth)
        elif isinstance(obj, property):
            for accessor in (obj.fget, obj.fset, obj.fdel):
                yield from code_of(accessor, depth)
        elif isinstance(obj, FunctionType):
            unwrapped = inspect.unwrap(obj)
            if unwrapped is not obj:  # e.g. a decorated function
                yield from code_of(unwrapped, depth)
            if obj.__module__ == module_name:
                yield obj.__code__
        elif isinstance(obj, type):
            if obj.__module__ == module_name:
                for member in list(vars(obj).values()):
                    yield from code_of(member, depth + 1)
        elif callable(obj) and (unwrapped := inspect.unwrap(obj)) is not obj:
            # e.g. a function cached by functools.cache
            yield from code_of(unwrapped, depth)

    for value in list(namespace.values()):
        try:
            yield from code_of(value, 0)
        except Exception:  # noqa: BLE001
            # e.g. an object that refuses to be inspected
            _logger.debug("Can't find the code of an object", exc_info=True)


class LiveSource:
    """
    Where the tests of a live run take the code of the kernel's notebook from (see
    ``SolutionSource``): the kernel's namespace, and the cells as they last ran.
    """

    def __init__(
        self,
        notebook: StrPath,
        namespace: Mapping[str, Any],
        log: CellLog,
        saved: _SavedNotebook | None = None,
    ):
        self.notebook = paths.absolute(notebook)
        self.namespace = namespace
        self.log = log
        self._saved = saved or _SavedNotebook(self.notebook)

    def covers(self, path: StrPath) -> bool:
        return _same(path) == _same(self.notebook)

    def module(self, trace: Trace | None = None) -> ModuleType:
        """A module of the kernel's objects, registered as ``current_solution`` like
        the module of an exercise file. With ``trace``, the functions and classes
        defined in the kernel are watched by it."""
        module = ModuleType(MODULE_NAME)
        module.__dict__.update(self.namespace)
        sys.modules[MODULE_NAME] = module
        if trace is not None:
            for code in _defined_code(self.namespace):
                trace.watch(code)
        return module

    def python_source(
        self, path: StrPath, *, section: str | None = None
    ) -> loader.NotebookSource:
        if not self.covers(path):
            return loader.python_source(path, section=section)
        return loader.source_of(self.log.code_cells(self._saved.read(), section))

    def cell_ref(self, cell_id: str | None) -> CellRef:
        """The cell ``cell_id`` that ran, with its position in the saved notebook if it
        is there."""
        if cell_id is None:
            return CellRef()
        try:
            cell = self.log.match(cells.cells(self._saved.read())).get(cell_id)
        except Exception:  # noqa: BLE001
            _logger.debug("Can't read the saved notebook", exc_info=True)
            cell = None
        return CellRef(cell_id, None if cell is None else cell.number)


class _LivePlugin(LiveTestCollectionPlugin):
    """``LiveTestCollectionPlugin`` that reports the test that was running when the
    kernel was interrupted as failed, as pytest reports nothing for it."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.interrupted = False
        self._started: tuple[str, float] | None = None

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self._started = (nodeid, time.perf_counter())

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        self._started = None

    def pytest_keyboard_interrupt(self, excinfo: Any) -> None:
        self.interrupted = True
        if self._started is None or self._index is None:
            return
        nodeid, start = self._started
        name = self.test_objs[self._index].name
        self.results.append(
            TestResult(
                nodeid,
                "call",
                "failed",
                time.perf_counter() - start,
                f"{escape(name)} failed: the kernel was interrupted while it ran, e.g. "
                "in an endless loop.",
                index=self._index,
            )
        )


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """
    Let interrupting the kernel raise ``KeyboardInterrupt`` while tests run: ipykernel
    ignores SIGINT outside of its handlers, and while a cell with top-level ``await``
    runs, it cancels the cell instead. Only the main thread can do so.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    if previous is None:  # not set from Python, so it couldn't be restored
        yield
        return
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _figure_numbers() -> set[int]:
    """The numbers of matplotlib's open figures, if pyplot is imported."""
    pyplot = sys.modules.get("matplotlib.pyplot")
    try:
        return set(pyplot.get_fignums()) if pyplot is not None else set()
    except Exception:  # noqa: BLE001
        return set()


def _close_figures(keep: set[int]) -> None:
    """Close matplotlib's figures but those numbered ``keep``, e.g. those the tests
    opened, which the inline backend would otherwise show below the next cell."""
    if (pyplot := sys.modules.get("matplotlib.pyplot")) is None:
        return
    for number in _figure_numbers() - keep:
        with contextlib.suppress(Exception):
            pyplot.close(number)


@contextlib.contextmanager
def _isolated(shell: Any) -> Iterator[None]:
    """
    Run tests in the kernel without anything reaching the notebook, and without
    changing what the student sees of the kernel: printed text is captured, displayed
    objects are dropped, the figures the tests open are closed, ``input()`` raises
    ``EOFError``, and ``sys.last_value`` (``%debug``) and ``current_solution`` are kept.
    """
    from IPython.core.displaypub import CapturingDisplayPublisher

    streams = sys.stdout, sys.stderr
    last_error = {
        name: getattr(sys, name) for name in _LAST_ERROR if hasattr(sys, name)
    }
    module = sys.modules.get(MODULE_NAME)
    display_pub = getattr(shell, "display_pub", None)
    figures = _figure_numbers()
    try:
        with (
            capture_stdout() as out,
            capture_stderr() as err,
            provide_input(()),
            _interruptible(),
        ):
            try:
                if display_pub is not None:
                    shell.display_pub = CapturingDisplayPublisher()
                yield
            finally:
                if display_pub is not None:
                    shell.display_pub = display_pub
        if output := out.getvalue() + err.getvalue():
            _logger.debug(f"Output of the live run:\n{output}")
    finally:
        _close_figures(figures)
        for name in _LAST_ERROR:
            if name in last_error:
                setattr(sys, name, last_error[name])
            elif hasattr(sys, name):
                delattr(sys, name)
        if module is None:
            sys.modules.pop(MODULE_NAME, None)
        else:
            sys.modules[MODULE_NAME] = module
        # the stand-ins that capture this thread's output (see ``capture_stdout``)
        # are taken out again, if the run put them in
        for name, stream in zip(("stdout", "stderr"), streams):
            current = getattr(sys, name)
            if current is not stream and getattr(current, "_real", None) is stream:
                setattr(sys, name, stream)


@dataclasses.dataclass
class _LoadedTask:
    """A task whose tests run live, loaded from its file."""

    task: Task
    # the course's requires its objects are decoded with (see ``ModulePolicy``), None
    # for any module
    requires: list[str] | None
    # the task file's modification time and size when it was loaded (see ``_version``)
    version: tuple[int, int]
    # the version of the task file that couldn't be loaded last, which isn't tried
    # again (nor reported again) until the file changes
    broken: tuple[int, int] | None = None


def _version(path: Path) -> tuple[int, int]:
    """The modification time and size of the file at ``path``, which tell whether it
    changed: its size changes when it is read while it is being written, even where
    modification times are coarse."""
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def _load_task(path: Path, requires: list[str] | None) -> _LoadedTask:
    """
    Load the task file ``path`` without creating any files.

    Raises
    ------
    Exception
        If it can't be loaded, e.g. ``OSError``, or ``ModuleNotAllowedError``.
    """
    version = _version(path)
    policy = None if requires is None else ModulePolicy(requires)
    task = Task(path, setup=False, module_policy=policy)
    return _LoadedTask(task, requires, version)


class _KernelLog(logging.Handler):
    """Passes the records of pyics's loggers on to the kernel's own log, which goes to
    the terminal Jupyter runs in, and never to the notebook."""

    def __init__(self, log: logging.Logger):
        super().__init__()
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        if self.log.isEnabledFor(record.levelno):
            self.log.handle(record)


class LiveRuns:
    """
    The extension in the IPython shell ``shell`` (see the module's documentation): it
    finds the notebook's tasks, and runs their tests after each cell.

    Parameters
    ----------
    shell: InteractiveShell
        The kernel's shell.
    clock: Callable[[], float] (optional, keyword only)
        The current time, ``time.monotonic`` by default.
    """

    def __init__(self, shell: Any, *, clock: Callable[[], float] = time.monotonic):
        self.shell = shell
        self.log = CellLog()
        # the notebook the kernel runs, once known
        self.notebook: Path | None = None
        # the tasks whose tests run, by task file
        self.tasks: dict[Path, _LoadedTask] = {}
        # whether the tasks were named by ``%pyics task``, rather than found
        self.named = False
        # paused by ``%pyics off``
        self.paused = False
        # the live runs so far
        self.runs = 0
        self.channel: KernelChannel | None = None
        self._registered: tuple[str, ...] | None = None
        self._saved: _SavedNotebook | None = None
        self._clock = clock
        self._searched: float | None = None
        self._warned: set[str] = set()
        self._running = False
        self._log_handler: logging.Handler | None = None
        self._propagate = True

    # --- loading ---

    def load(self) -> None:
        """Start: route pyics's logs to the kernel's log, register the magic and the
        event handler, and find the notebook's tasks if the notebook is known."""
        self._route_logs()
        self.shell.register_magic_function(self.magic, "line", "pyics")
        self.shell.events.register("post_run_cell", self.post_run_cell)
        try:
            self._update()
        except Exception:  # noqa: BLE001
            _logger.warning("Can't find the notebook's tasks", exc_info=True)

    def unload(self) -> None:
        """Stop: unregister the event handler and the magic, and close the channel."""
        with contextlib.suppress(ValueError):
            self.shell.events.unregister("post_run_cell", self.post_run_cell)
        with contextlib.suppress(Exception):
            self.shell.magics_manager.magics["line"].pop("pyics", None)
        self._close_channel()
        if self._log_handler is not None:
            pyics_logger = logging.getLogger("pyics")
            pyics_logger.removeHandler(self._log_handler)
            pyics_logger.propagate = self._propagate
            self._log_handler = None

    def _route_logs(self) -> None:
        """Send the records of pyics's loggers to the kernel's own log rather than to
        the root logger, which the student may have set up to show records in the
        notebook (and which shows warnings on ``sys.stderr`` if not set up)."""
        kernel_log = getattr(getattr(self.shell, "kernel", None), "log", None)
        handler: logging.Handler = (
            _KernelLog(kernel_log)
            if isinstance(kernel_log, logging.Logger)
            else logging.NullHandler()
        )
        pyics_logger = logging.getLogger("pyics")
        self._propagate = pyics_logger.propagate
        pyics_logger.addHandler(handler)
        pyics_logger.propagate = False
        self._log_handler = handler

    # --- the notebook and its tasks ---

    def _update(self) -> None:
        """Follow the notebook's path, find its tasks (again, if none were found a while
        ago), reload those whose files changed, and announce them on the channel."""
        notebook = notebook_path(self.shell.user_ns, os.getcwd())
        if notebook is not None and (
            self.notebook is None or _same(notebook) != _same(self.notebook)
        ):
            self._set_notebook(notebook)
        if not self.named and self.notebook is not None:
            searched = self._searched
            if not self.tasks and (
                searched is None or self._clock() - searched >= SEARCH_AGAIN_AFTER
            ):
                self._searched = self._clock()
                self.tasks = self._load_all(find_tasks(self.notebook))
        self._reload_changed()
        self._register()

    def _set_notebook(self, notebook: Path) -> None:
        """Switch to the notebook ``notebook``, whose tasks are searched for anew."""
        self._close_channel()
        self.notebook = notebook
        self._saved = _SavedNotebook(notebook)
        if not self.named:
            self.tasks = {}
            self._searched = None

    def _load_all(
        self, task_files: Mapping[Path, list[str] | None]
    ) -> dict[Path, _LoadedTask]:
        """The tasks of ``task_files``, with their courses' requires, which can be
        loaded."""
        loaded = {}
        for path, requires in task_files.items():
            try:
                loaded[path] = _load_task(path, requires)
            except Exception:  # noqa: BLE001
                _logger.warning(f"Can't load the task {path}", exc_info=True)
        return loaded

    def _reload_changed(self) -> None:
        """Load the tasks whose files changed again, e.g. after ``pyics update``. A task
        that can't be loaded keeps its tests until it can, and one whose file was
        removed is dropped."""
        for path, loaded in list(self.tasks.items()):
            try:
                version = _version(path)
            except FileNotFoundError:
                _logger.warning(f"The task file {path} was removed")
                del self.tasks[path]
                continue
            except OSError:
                continue
            if version in (loaded.version, loaded.broken):
                continue
            try:
                self.tasks[path] = _load_task(path, loaded.requires)
            except Exception:  # noqa: BLE001
                loaded.broken = version
                _logger.warning(f"Can't load the changed task {path}", exc_info=True)

    def _register(self) -> None:
        """Announce the kernel and its tasks on the channel of its notebook, when they
        changed (see ``KernelChannel.register``)."""
        if not self.tasks:
            self._close_channel()
            return
        if self.notebook is None:
            return
        task_files = tuple(str(path) for path in self.tasks)
        if self.channel is not None and self._registered == task_files:
            return
        if self.channel is None:
            self.channel = KernelChannel(
                self.notebook, connection_file=channel.current_connection_file()
            )
        self.channel.register(self.tasks)
        self._registered = task_files

    def _close_channel(self) -> None:
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:  # noqa: BLE001
                _logger.warning("Can't close the channel", exc_info=True)
        self.channel = None
        self._registered = None

    def _warn_no_task(self) -> None:
        """Say once per notebook that no task was found for it, and how to name one."""
        if self.notebook is None or self.tasks or self.named:
            return
        if (key := _same(self.notebook)) in self._warned:
            return
        self._warned.add(key)
        print(NO_TASK_WARNING.format(name=self.notebook.name), file=sys.stderr)

    # --- after each cell ---

    def post_run_cell(self, result: Any) -> None:
        """
        After a cell ran (IPython's ``post_run_cell`` event): remember its source, and
        run the tests of the notebook's tasks on the kernel's objects, unless paused.
        Only cells that the frontend ran count, i.e. not code run by the kernel or by
        other code (whose history isn't stored). Never raises: errors are logged.
        """
        info = getattr(result, "info", None)
        if self._running or info is None or not getattr(info, "store_history", False):
            return
        self._running = True
        try:
            cell_id = getattr(info, "cell_id", None)
            cell_id = cell_id if isinstance(cell_id, str) else None
            if isinstance(info.raw_cell, str):
                self.log.record(cell_id, info.raw_cell)
            self._update()
            self._warn_no_task()
            if not self.paused:
                self._live_run(cell_id)
        except KeyboardInterrupt:
            _logger.info("The live run was interrupted")
        except Exception:  # noqa: BLE001
            _logger.warning("The live run failed", exc_info=True)
        finally:
            self._running = False

    def _live_run(self, cell_id: str | None) -> None:
        """Run the tests of the tasks on the kernel's objects, and write their results
        to the channel, unless the TUI paused live runs."""
        if self.channel is None or self.notebook is None or not self.tasks:
            return
        control = self.channel.read_control()
        if control.paused:
            return
        assert self._saved is not None
        source = LiveSource(self.notebook, self.shell.user_ns, self.log, self._saved)
        self.runs += 1
        results: dict[Path, list[TestResult]] = {}
        token = SOLUTION_SOURCE.set(source)
        try:
            with _isolated(self.shell):
                for path, loaded in list(self.tasks.items()):
                    plugin = _LivePlugin(
                        loaded.task.tests,
                        tracing=control.tracing,
                        sections=loaded.task.sections,
                    )
                    code = pytest.main(list(PYTEST_ARGS), plugins=[plugin])
                    if code in (
                        pytest.ExitCode.INTERNAL_ERROR,
                        pytest.ExitCode.USAGE_ERROR,
                    ):
                        _logger.warning(
                            f"pytest couldn't run the tests of {path}: {code!r}"
                        )
                    results[path] = plugin.results
                    if plugin.interrupted:
                        _logger.info("The live run was interrupted")
                        break
        finally:
            SOLUTION_SOURCE.reset(token)
        self.channel.write_results(self.runs, source.cell_ref(cell_id), results)

    # --- %pyics ---

    def magic(self, line: str) -> None:
        """
        ``%pyics task <task file>`` runs the tests of the task in the task file (rather
        than those of the tasks found for the notebook) when cells run; ``%pyics off``
        and ``%pyics on`` pause and resume live runs. Prints nothing.
        """
        from IPython.core.error import UsageError

        command, _, argument = line.strip().partition(" ")
        argument = argument.strip()
        if command in ("off", "on") and not argument:
            self.paused = command == "off"
        elif command == "task" and argument:
            self._name_task(_unquote(argument))
        else:
            raise UsageError(f"Usage: {USAGE}")

    def _name_task(self, task_file: str) -> None:
        """Run the tests of the task in ``task_file`` from now on (see ``magic``)."""
        from IPython.core.error import UsageError

        path = paths.absolute(Path(task_file).expanduser())
        try:
            loaded = _load_task(path, _course_requires(path))
        except Exception as err:  # noqa: BLE001
            raise UsageError(f"Can't load the task {path}: {err}") from None
        self.named = True
        self.tasks = {path: loaded}
        if self.notebook is None:
            notebooks = [f for f in loaded.task.exercise_files if cells.is_notebook(f)]
            if notebooks:
                self._set_notebook(paths.absolute(notebooks[0]))
        try:
            self._register()
        except Exception:  # noqa: BLE001
            _logger.warning("Can't announce the kernel on the channel", exc_info=True)


def _unquote(text: str) -> str:
    """``text`` without the quotes around it, if it is quoted."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


# the extension in each shell it is loaded in
_extensions: weakref.WeakKeyDictionary[Any, LiveRuns] = weakref.WeakKeyDictionary()


def load_ipython_extension(ip: Any) -> None:
    """Load the extension into the shell ``ip`` (see ``LiveRuns``), unless it is loaded
    already."""
    if ip in _extensions:
        return
    extension = LiveRuns(ip)
    _extensions[ip] = extension
    try:
        extension.load()
    except Exception:  # noqa: BLE001
        _logger.warning("Can't load the pyics extension", exc_info=True)


def unload_ipython_extension(ip: Any) -> None:
    """Unload the extension from the shell ``ip``, if it is loaded."""
    if (extension := _extensions.pop(ip, None)) is not None:
        extension.unload()


def loaded(ip: Any) -> LiveRuns | None:
    """The extension loaded into the shell ``ip``, if it is."""
    return _extensions.get(ip)
