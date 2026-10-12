"""
The IPython extension that runs the tests of a notebook's tasks whenever one of its
cells runs ("live runs"), and writes their results to the notebook's channel (see
``channel``), where pyics shows them (see ``overlay``). The "Python (pyics)" kernel
loads it at every start (``--ext=pyics_jupyter``, see ``kernelspec``); any other IPython
kernel loads it with ``%load_ext pyics_jupyter``, and ``%unload_ext pyics_jupyter``
stops it.

**Finding the tasks.** The notebook's path comes from ``__session__`` (set by ipykernel
from ``JPY_SESSION_NAME``, which Jupyter's server sets) or ``__vsc_ipynb_file__`` (set
by VS Code once the kernel runs, so it is looked for at each cell run). Its tasks are
those of the courses in its directory, in the ``PARENT_LEVELS`` directories above it,
and in their subdirectories, that list the notebook as an exercise file (see
``find_tasks``). They are loaded once: restart the kernel after ``pyics update``.

**Live runs.** After each cell (IPython's ``post_run_cell`` event), the tests run in the
kernel with ``LIVE_SOURCE`` set: function and spec tests test the objects as the kernel
defines them, while output and structure tests are skipped, as they run on the saved
notebook.

**Silence.** Nothing of a live run reaches the notebook: what the tested code prints is
captured, what it displays is dropped, ``input()`` raises ``EOFError``, and pyics's logs
go nowhere. Interrupting the kernel stops a live run, e.g. an endless loop in a tested
function, whose test fails. Errors are never raised into the kernel. The only output is
a one-line warning if no task is found for the notebook.
"""

import contextlib
import logging
import os
import signal
import sys
import threading
import time
import weakref
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from rich.markup import escape

from pyics.data.task import Task
from pyics.distribution.dependencies import ModulePolicy, manifest_requirements
from pyics.notebook import cells
from pyics.tests.capture import capture_stderr, capture_stdout, provide_input
from pyics.tests.pytest_integration import LiveTestCollectionPlugin
from pyics.tests.results import TestResult
from pyics.tests.tests import LIVE_SOURCE
from pyics.util import json_handling as json
from pyics.util import paths
from pyics.util.discovery import find_courses
from pyics.util.paths import StrPath
from pyics_jupyter.channel import KernelChannel, same_file

# pytest's arguments for live runs. Besides those of the TUI's runs, they keep the
# student's and the course's pytest setup (conftest files, plugins, ``addopts``) out of
# the kernel, and leave out the plugins that change the process's state while pytest
# runs (faulthandler, ``sys.unraisablehook``, ``threading.excepthook``).
PYTEST_ARGS = (
    *("-p", "no:terminal", "-p", "no:cacheprovider", "--capture=no", "--noconftest"),
    *("--disable-plugin-autoload", "-o", "addopts="),
    *("-p", "no:faulthandler", "-p", "no:unraisableexception"),
    *("-p", "no:threadexception"),
)
# how many directories above the notebook's are searched for courses
PARENT_LEVELS = 3
# the name of the module of the kernel's objects while tests run, like the module of an
# exercise file (see ``Test.load_solution``)
MODULE_NAME = "current_solution"
NO_TASK_WARNING = (
    "pyics: no task found for {name} (no course near it lists it as an exercise "
    "file), so no tests run when its cells run."
)
# the attributes of ``sys`` that pytest sets when a test fails, which would make
# ``%debug`` debug the test rather than the student's error
_LAST_ERROR = ("last_type", "last_value", "last_traceback", "last_exc")
# silenced while the extension is loaded, so that no log record reaches the notebook
_LOGGERS = ("pyics", "pyics_jupyter")

_logger = logging.getLogger(__name__)


def notebook_path(namespace: Mapping[str, Any], cwd: StrPath) -> Path | None:
    """
    The path of the notebook a kernel runs, by the variables its frontend sets in the
    kernel's ``namespace``: ``__vsc_ipynb_file__`` (VS Code), or ``__session__``
    (ipykernel, from Jupyter's ``JPY_SESSION_NAME``). None if neither names a notebook.
    A relative path is looked up in the kernel's working directory ``cwd``, which is
    the notebook's directory (older Jupyter servers give it relative to their root).
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


def _lists(task_file: Path, notebook: str) -> bool:
    """Whether the task file ``task_file``, read as plain data (so that nothing is
    decoded), lists the notebook ``notebook`` (see ``same_file``) as an exercise
    file."""
    try:
        ex_files = json.load_plain(task_file).get("ex_files") or []
        base = task_file.parent
        return any(same_file(paths.resolve(f, base)) == notebook for f in ex_files)
    except Exception:  # noqa: BLE001
        return False


def find_tasks(notebook: Path, *, levels: int = PARENT_LEVELS) -> dict[Path, list[str]]:
    """
    The task files that list ``notebook`` as an exercise file, of the courses whose
    manifests are in the notebook's directory, in one of the ``levels`` directories
    above it, or in one of their subdirectories, each with the ``requires`` of its
    course (see ``ModulePolicy``).
    """
    wanted = same_file(notebook)
    directory = paths.absolute(notebook).parent
    found: dict[Path, list[str]] = {}
    for root in [directory, *list(directory.parents)[:levels]]:
        for manifest_path, manifest in find_courses(root, max_depth=1).items():
            try:
                requires, _ = manifest_requirements(manifest)
            except ValueError:
                continue
            for task_file in manifest["tasks"].values():
                path = paths.resolve(task_file, manifest_path.parent)
                if path not in found and _lists(path, wanted):
                    found[path] = requires
    return found


def _cell_number(notebook: Path, cell_id: object) -> int | None:
    """The position (from 1) of the cell ``cell_id`` in the saved ``notebook``, if it is
    there: frontends such as VS Code give cells ids of their own."""
    if not isinstance(cell_id, str):
        return None
    try:
        saved = cells.cells(cells.read(notebook))
    except OSError, ValueError:
        return None
    return next((cell.number for cell in saved if cell.id == cell_id), None)


class _LiveSource:
    """The objects the kernel defines (its namespace), as the module of ``notebook``
    (see ``LIVE_SOURCE``)."""

    def __init__(self, notebook: Path, namespace: Mapping[str, Any]):
        self.notebook = same_file(notebook)
        self.namespace = namespace

    def module(self, path: StrPath) -> ModuleType | None:
        if same_file(path) != self.notebook:
            return None
        module = ModuleType(MODULE_NAME)
        module.__dict__.update(self.namespace)
        sys.modules[MODULE_NAME] = module
        return module


class _LivePlugin(LiveTestCollectionPlugin):
    """``LiveTestCollectionPlugin`` that reports the test that was running when the
    kernel was interrupted as failed, as pytest reports nothing for it."""

    interrupted = False
    _started: tuple[str, float] | None = None

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self._started = (nodeid, time.perf_counter())

    def pytest_runtest_logfinish(self) -> None:
        self._started = None

    def pytest_keyboard_interrupt(self) -> None:
        self.interrupted = True
        if self._started is None or self._index is None:
            return
        nodeid, start = self._started
        message = (
            f"{escape(self.test_objs[self._index].name)} failed: the kernel was "
            "interrupted while it ran, e.g. in an endless loop."
        )
        duration = time.perf_counter() - start
        self.results.append(
            TestResult(nodeid, "call", "failed", duration, message, index=self._index)
        )


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """Let interrupting the kernel raise ``KeyboardInterrupt`` while tests run: ipykernel
    ignores SIGINT outside of its handlers, and cancels a cell with top-level ``await``
    instead. Only the main thread can do so."""
    previous = signal.getsignal(signal.SIGINT)
    if threading.current_thread() is not threading.main_thread() or previous is None:
        yield
        return
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def _isolated(shell: Any) -> Iterator[None]:
    """Run tests without anything reaching the notebook: printed text is captured,
    displayed objects are dropped, ``input()`` raises ``EOFError``, and the error that
    ``%debug`` shows and ``current_solution`` stay as they were."""
    from IPython.core.displaypub import CapturingDisplayPublisher

    streams = sys.stdout, sys.stderr
    last_error = {
        name: getattr(sys, name) for name in _LAST_ERROR if hasattr(sys, name)
    }
    module = sys.modules.get(MODULE_NAME)
    display_pub = shell.display_pub
    shell.display_pub = CapturingDisplayPublisher()
    try:
        with capture_stdout(), capture_stderr(), provide_input(()), _interruptible():
            yield
    finally:
        shell.display_pub = display_pub
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
        for name, stream in zip(("stdout", "stderr"), streams):
            if getattr(getattr(sys, name), "_real", None) is stream:
                setattr(sys, name, stream)


class LiveRuns:
    """The extension in the IPython shell ``shell`` (see the module's documentation)."""

    def __init__(self, shell: Any):
        self.shell = shell
        # the notebook the kernel runs, once known, its tasks, and its channel (if it
        # has tasks)
        self.notebook: Path | None = None
        self.tasks: dict[Path, Task] = {}
        self.channel: KernelChannel | None = None
        self._warned = False
        self._running = False
        self._loggers: dict[str, tuple[logging.Handler, bool]] = {}

    def load(self) -> None:
        """Silence pyics's logs, follow the cells the notebook runs, and find its tasks
        if the notebook is known."""
        for name in _LOGGERS:
            logger, handler = logging.getLogger(name), logging.NullHandler()
            self._loggers[name] = (handler, logger.propagate)
            logger.addHandler(handler)
            logger.propagate = False
        self.shell.events.register("post_run_cell", self.post_run_cell)
        try:
            self._find_tasks()
        except Exception:  # noqa: BLE001
            _logger.warning("Can't find the notebook's tasks", exc_info=True)

    def unload(self) -> None:
        """Stop following cells, close the channel, and let pyics log again."""
        with contextlib.suppress(ValueError):
            self.shell.events.unregister("post_run_cell", self.post_run_cell)
        self._close()
        for name, (handler, propagate) in self._loggers.items():
            logging.getLogger(name).removeHandler(handler)
            logging.getLogger(name).propagate = propagate
        self._loggers.clear()

    def _close(self) -> None:
        if self.channel is not None:
            with contextlib.suppress(Exception):
                self.channel.close()
        self.channel = None

    def _find_tasks(self) -> None:
        """If the kernel names another notebook than before (see ``notebook_path``),
        load its tasks, and open its channel if it has any."""
        notebook = notebook_path(self.shell.user_ns, os.getcwd())
        if notebook is None or (
            self.notebook is not None
            and same_file(notebook) == same_file(self.notebook)
        ):
            return
        self._close()
        self.notebook, self.tasks, self._warned = notebook, {}, False
        for path, requires in find_tasks(notebook).items():
            try:
                policy = ModulePolicy(requires)
                self.tasks[path] = Task(path, setup=False, module_policy=policy)
            except Exception:  # noqa: BLE001
                _logger.warning(f"Can't load the task {path}", exc_info=True)
        if self.tasks:
            self.channel = KernelChannel(notebook)
            self.channel.open()

    def post_run_cell(self, result: Any) -> None:
        """
        After a cell ran (IPython's ``post_run_cell`` event), run the tests of the
        notebook's tasks on the kernel's objects, and write their results to the
        channel. Only cells the frontend ran count, not code run by the kernel or by
        other code (whose history isn't stored). Never raises.
        """
        info = getattr(result, "info", None)
        if self._running or not getattr(info, "store_history", False):
            return
        self._running = True
        try:
            self._find_tasks()
            if self.notebook is not None and not self.tasks and not self._warned:
                self._warned = True
                print(NO_TASK_WARNING.format(name=self.notebook.name), file=sys.stderr)
            if self.channel is not None and self.notebook is not None:
                results = self._live_run(self.notebook)
                cell = _cell_number(self.notebook, getattr(info, "cell_id", None))
                self.channel.write(results, cell)
        except KeyboardInterrupt:
            _logger.info("The live run was interrupted")
        except Exception:  # noqa: BLE001
            _logger.warning("The live run failed", exc_info=True)
        finally:
            self._running = False

    def _live_run(self, notebook: Path) -> dict[Path, list[TestResult]]:
        """The results of the tasks' tests on the kernel's objects, by task file. If
        the kernel is interrupted, the tasks after the one that ran are left out."""
        results: dict[Path, list[TestResult]] = {}
        token = LIVE_SOURCE.set(_LiveSource(notebook, self.shell.user_ns))
        try:
            with _isolated(self.shell):
                for path, task in self.tasks.items():
                    plugin = _LivePlugin(task.tests, sections=task.sections)
                    pytest.main(list(PYTEST_ARGS), plugins=[plugin])
                    results[path] = plugin.results
                    if plugin.interrupted:
                        break
        finally:
            LIVE_SOURCE.reset(token)
        return results


# the extension in each shell it is loaded in
_loaded: weakref.WeakKeyDictionary[Any, LiveRuns] = weakref.WeakKeyDictionary()


def load_ipython_extension(ip: Any) -> None:
    """Load the extension into the shell ``ip``, unless it is loaded already."""
    if ip not in _loaded:
        _loaded[ip] = LiveRuns(ip)
        _loaded[ip].load()


def unload_ipython_extension(ip: Any) -> None:
    """Unload the extension from the shell ``ip``, if it is loaded."""
    if (extension := _loaded.pop(ip, None)) is not None:
        extension.unload()
