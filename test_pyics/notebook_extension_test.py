"""
The IPython extension that runs the tests of a notebook's tasks whenever a cell runs
(``pyics.notebook.extension``), driven through a real ``InteractiveShell``: finding
the tasks, live results written to the channel, silent runs, sections, ``%pyics``, and
interrupting a run.
"""

import _thread
import atexit
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import CapturedIO, capture_output
from notebook_helpers import CellData, markdown, write_notebook
from traitlets.config import Config

import pyics
from pyics.data import Course, Task
from pyics.notebook import extension
from pyics.notebook.cells import Cell
from pyics.notebook.channel import (
    CONTROL_FILE,
    CellRef,
    LiveResults,
    channel_dir,
    read_json,
    write_json_atomic,
)
from pyics.notebook.extension import CellLog, LiveRuns, find_tasks, notebook_path
from pyics.tests.results import TestResult
from pyics.tests.tests import PrintTest, SimpleTest, StructureTest, Test
from pyics.tests.trace import ansi_text

ADD_CELLS: list[str | CellData] = [
    markdown("# Adding"),
    "def add(a, b):\n    return 0\n",
]
ADD = "def add(a, b):\n    return a + b\n"
SUBTRACT = "def add(a, b):\n    return a - b\n"


@pytest.fixture
def shell(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[InteractiveShell]:
    """IPython's shell, as a kernel runs it, without history or profile of the user.
    The extension, the shell and what the shell changes are cleaned up afterwards."""
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path_factory.mktemp("ipython")))
    main = sys.modules.get("__main__")
    config = Config()
    config.HistoryManager.hist_file = ":memory:"
    ip = InteractiveShell.instance(config=config)
    try:
        yield ip
    finally:
        extension.unload_ipython_extension(ip)
        atexit.unregister(ip.atexit_operations)
        InteractiveShell.clear_instance()
        sys.modules.pop("current_solution", None)
        if main is not None:
            sys.modules["__main__"] = main


@pytest.fixture
def extension_log() -> Iterator[list[logging.LogRecord]]:
    """The records the extension logs (which it keeps out of the root logger)."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("pyics.notebook.extension")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(level)


def write_task(
    root: Path,
    cells: Sequence[str | CellData],
    tests: Callable[[Path], list[Test]],
    *,
    name: str = "add",
    sections: dict[Path, str] | None = None,
    course: bool = True,
) -> tuple[Path, Path]:
    """Write a task whose exercise is a notebook of ``cells`` (``<name>.ipynb`` in
    ``root``) with the ``tests`` of that notebook, and (with ``course``) a course of it
    in ``root``. Returns the notebook's and the task file's paths."""
    notebook = write_notebook(root / f"{name}.ipynb", cells)
    solution = write_notebook(root / f".solutions/{name}.ipynb", cells)
    task = Task.create_task_json(
        root / f".tasks/{name}",
        name.title(),
        f"Implement {name}.",
        notebook,
        solution,
        tests(notebook),
        sections=sections,
    )
    if course:
        Course.create_course_manifest(root / "course", [task], name="Course")
    return notebook, task


def adds(notebook: Path) -> list[Test]:
    return [SimpleTest("adds", notebook, "add", 3, function_args=[1, 2])]


def load(
    ip: InteractiveShell, notebook: Path | None, monkeypatch: pytest.MonkeyPatch
) -> LiveRuns:
    """Load the extension into ``ip`` as a kernel for ``notebook`` (if given) would,
    in the notebook's directory."""
    if notebook is not None:
        ip.user_ns["__session__"] = str(notebook)
        monkeypatch.chdir(notebook.parent)
    pyics.load_ipython_extension(ip)
    loaded = extension.loaded(ip)
    assert loaded is not None
    return loaded


def run(ip: InteractiveShell, source: str, cell_id: str | None = None) -> CapturedIO:
    """Run ``source`` as the frontend runs a cell, and return what the cell shows."""
    with capture_output() as captured:
        ip.run_cell(source, store_history=True, cell_id=cell_id)
    return captured


def assert_silent(captured: CapturedIO) -> None:
    assert captured.stdout == ""
    assert captured.stderr == ""
    assert captured.outputs == []


def live_results(live: LiveRuns) -> LiveResults:
    assert live.channel is not None
    data = read_json(live.channel.results_file)
    assert data is not None, "no live results were written"
    return LiveResults.from_json(data, live.channel.kernel_id)


def calls(live: LiveRuns, task: Path) -> dict[str, TestResult]:
    """The results of the call phases of the tests of ``task`` in the latest live run,
    by test name."""
    results = live_results(live).for_task(task)
    assert results is not None, f"no live results for {task}"
    return {r.nodeid.removeprefix("::"): r for r in results if r.when == "call"}


def outcomes(live: LiveRuns, task: Path) -> dict[str, str]:
    return {name: r.outcome for name, r in calls(live, task).items()}


# --- live results ---


def test_defining_a_function_writes_live_results(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    assert list(live.tasks) == [task]
    assert live.channel is not None
    assert live.channel.directory == channel_dir(notebook)
    assert read_json(live.channel.kernel_file)["task_files"] == [task.as_posix()]

    assert_silent(run(shell, ADD, "cell-2"))
    results = live_results(live)
    assert results.cell == CellRef("cell-2", 2)
    assert outcomes(live, task) == {"adds": "passed"}
    first_run = results.run

    assert_silent(run(shell, SUBTRACT, "cell-2"))
    results = live_results(live)
    assert results.run == first_run + 1
    result = calls(live, task)["adds"]
    assert result.outcome == "failed"
    assert result.message == "adds failed: -1 == 3"
    assert result.index == 0
    assert result.trace is None


def test_the_cell_output_stays_empty_while_tested_code_prints_and_displays(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    stdout, stderr = sys.stdout, sys.stderr

    captured = run(
        shell,
        "import sys, warnings\n"
        "from IPython.display import display\n"
        "def add(a, b):\n"
        "    print('adding', a, b)\n"
        "    print('to stderr', file=sys.stderr)\n"
        "    display({'a': a})\n"
        "    warnings.warn('careful')\n"
        "    return a + b\n"
        "print('the cell itself')\n",
        "cell-2",
    )
    assert captured.stdout == "the cell itself\n"
    assert captured.stderr == ""
    assert captured.outputs == []
    assert outcomes(live, task) == {"adds": "passed"}
    # the streams are as they were, and so is the shell's display publisher
    assert (sys.stdout, sys.stderr) == (stdout, stderr)
    assert "Capturing" not in type(shell.display_pub).__name__


def test_print_tests_are_skipped_in_live_runs(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tests(notebook: Path) -> list[Test]:
        return [PrintTest("prints", notebook, "3\n"), *adds(notebook)]

    notebook, task = write_task(tmp_path, [*ADD_CELLS, "print(add(1, 2))"], tests)
    live = load(shell, notebook, monkeypatch)
    assert_silent(run(shell, ADD, "cell-2"))
    results = calls(live, task)
    assert {name: r.outcome for name, r in results.items()} == {
        "prints": "skipped",
        "adds": "passed",
    }
    assert results["prints"].index == 0
    assert results["prints"].message == "Skipped: runs when the notebook is saved"


def test_tracing_turned_on_by_the_tui_traces_failures(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    run(shell, SUBTRACT, "cell-2")
    assert calls(live, task)["adds"].trace is None

    write_json_atomic(channel_dir(notebook) / CONTROL_FILE, {"tracing": True})
    assert_silent(run(shell, SUBTRACT, "cell-2"))
    result = calls(live, task)["adds"]
    assert result.outcome == "failed"
    assert result.trace is not None
    assert "return a - b" in ansi_text(result.trace).plain


def test_a_structure_test_with_a_scope_checks_the_latest_run_source(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tests(notebook: Path) -> list[Test]:
        rule = {"concept": "recursion", "scope": "fib"}
        return [StructureTest("recursive", notebook, rule, section=None)]

    notebook, task = write_task(tmp_path, ["def fib(n):\n    return n\n"], tests)
    live = load(shell, notebook, monkeypatch)
    recursive = "def fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n"
    run(shell, recursive, "cell-1")
    assert outcomes(live, task) == {"recursive": "passed"}
    run(shell, "def fib(n):\n    return n\n", "cell-1")
    assert outcomes(live, task) == {"recursive": "failed"}


def test_a_structure_test_checks_the_section_as_its_cells_were_last_run(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cells = [
        markdown("<!-- pyics: ex1 -->\n# Exercise 1"),
        "def f():\n    return 1\n",
        markdown("<!-- pyics: ex2 -->\n# Exercise 2"),
        "def g():\n    return 2\n",
    ]

    def tests(notebook: Path) -> list[Test]:
        return [StructureTest("no loops", notebook, {"concept": "loop", "max": 0})]

    notebook = tmp_path / "add.ipynb"
    notebook, task = write_task(tmp_path, cells, tests, sections={notebook: "ex2"})
    live = load(shell, notebook, monkeypatch)

    loop = "def {}():\n    for i in range(3):\n        pass\n"
    run(shell, loop.format("g"), "cell-4")
    result = calls(live, task)["no loops"]
    assert result.outcome == "failed"
    assert "(cell 4, line 2)" in result.message_text.plain

    # cells of other sections don't count
    run(shell, "def g():\n    return 2\n", "cell-4")
    run(shell, loop.format("f"), "cell-2")
    assert outcomes(live, task) == {"no loops": "passed"}

    # a new cell that ran after a cell of the section is part of it
    run(shell, "def g():\n    return 2\n", "cell-4")
    run(shell, loop.format("h"), "new-cell")
    result = calls(live, task)["no loops"]
    assert result.outcome == "failed"
    assert "(cell 5, line 2)" in result.message_text.plain

    # without the section's anchor in the saved notebook, the test says so
    write_notebook(notebook, cells[:2] + [markdown("# Exercise 2")] + cells[3:])
    run(shell, "x = 1", "cell-5")
    result = calls(live, task)["no loops"]
    assert result.outcome == "failed"
    assert "Section 'ex2' not found" in result.message_text.plain


def test_input_in_tested_code_does_not_wait_for_an_answer(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    captured = run(shell, "def add(a, b):\n    return int(input('a? ')) + b\n", "c")
    assert_silent(captured)
    result = calls(live, task)["adds"]
    assert result.outcome == "failed"
    assert "EOF" in result.message_text.plain


def test_a_live_run_keeps_the_error_debug_would_show(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    run(shell, "1 / 0", "cell-3")
    assert isinstance(sys.last_value, ZeroDivisionError)
    run(shell, SUBTRACT, "cell-2")
    assert outcomes(live, task) == {"adds": "failed"}
    assert isinstance(sys.last_value, ZeroDivisionError)
    assert "current_solution" not in sys.modules


# --- finding the notebook and its tasks ---


def test_tasks_are_found_through_the_courses_near_the_notebook(tmp_path: Path) -> None:
    root = tmp_path / "course"
    notebook, task = write_task(root, ADD_CELLS, adds)
    # three directories below the course's manifest, and four
    near, near_task = write_task(
        root / "x/y/z", ADD_CELLS, adds, name="near", course=False
    )
    far, far_task = write_task(
        root / "w/x/y/z", ADD_CELLS, adds, name="far", course=False
    )
    # a task of another notebook, which would fail to load: it is never decoded
    other = root / ".tasks/other.pyicstask.json"
    other.write_text(
        '{"name": "Other", "ex_files": ["other.ipynb"], "sol_files": [], '
        '"exercise_code": [], "solution_code": [], '
        '"tests": [{"__instance_type__": ["not_a_module_anywhere", "Test"], '
        '"attributes": {}}]}',
        encoding="utf-8",
    )
    Course.create_course_manifest(
        root / "course", [task, near_task, far_task, other], name="Course"
    )

    assert find_tasks(notebook) == {task: []}
    assert find_tasks(near) == {near_task: []}
    assert find_tasks(far) == {}
    assert find_tasks(root / "elsewhere.ipynb") == {}


def test_the_notebook_comes_from_the_session_or_vs_code(tmp_path: Path) -> None:
    notebook = write_notebook(tmp_path / "sub/add.ipynb", ADD_CELLS)
    assert notebook_path({}, tmp_path) is None
    assert notebook_path({"__session__": "console-1"}, tmp_path) is None
    assert notebook_path({"__session__": str(notebook)}, tmp_path) == notebook
    # relative to the server's root, while the kernel runs in the notebook's directory
    assert notebook_path({"__session__": "sub/add.ipynb"}, notebook.parent) == notebook
    assert notebook_path({"__session__": "sub/add.ipynb"}, tmp_path) == notebook
    vs_code = {"__session__": "x.ipynb", "__vsc_ipynb_file__": str(notebook)}
    assert notebook_path(vs_code, tmp_path) == notebook


def test_vs_codes_notebook_variable_is_looked_for_at_each_cell(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    monkeypatch.chdir(tmp_path)
    live = load(shell, None, monkeypatch)
    assert_silent(run(shell, ADD, "cell-2"))
    assert live.notebook is None and live.channel is None

    shell.user_ns["__vsc_ipynb_file__"] = str(notebook)
    assert_silent(run(shell, ADD, "cell-2"))
    assert live.notebook == notebook
    assert outcomes(live, task) == {"adds": "passed"}


def test_without_a_task_a_single_warning_says_how_to_name_one(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, _ = write_task(tmp_path, ADD_CELLS, adds, course=False)
    live = load(shell, notebook, monkeypatch)
    captured = run(shell, ADD, "cell-2")
    assert captured.stdout == "" and captured.outputs == []
    assert captured.stderr.count("\n") == 1
    assert "add.ipynb" in captured.stderr
    assert "%pyics task" in captured.stderr
    assert live.channel is None
    assert not channel_dir(notebook).exists()

    assert_silent(run(shell, ADD, "cell-2"))
    assert_silent(run(shell, "x = 1", "cell-3"))


def test_pyics_task_names_the_task(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds, course=False)
    live = load(shell, notebook, monkeypatch)
    relative = os.path.relpath(task, notebook.parent)
    assert_silent(run(shell, f'%pyics task "{relative}"', "cell-9"))
    assert list(live.tasks) == [task]

    assert_silent(run(shell, ADD, "cell-2"))
    assert outcomes(live, task) == {"adds": "passed"}


def test_pyics_task_finds_the_notebook_if_the_kernel_does_not_know_it(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds, course=False)
    monkeypatch.chdir(tmp_path)
    live = load(shell, None, monkeypatch)
    assert_silent(run(shell, f"%pyics task {task}", "cell-9"))
    assert live.notebook == notebook
    assert_silent(run(shell, ADD, "cell-2"))
    assert outcomes(live, task) == {"adds": "passed"}


def test_pyics_task_with_a_wrong_file_says_so_without_a_traceback(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, _ = write_task(tmp_path, ADD_CELLS, adds)
    load(shell, notebook, monkeypatch)
    captured = run(shell, "%pyics task missing.pyicstask.json", "cell-9")
    assert "UsageError" in captured.stderr
    assert "Traceback" not in captured.stderr + captured.stdout
    captured = run(shell, "%pyics", "cell-9")
    assert "Usage: %pyics task" in captured.stderr


def test_changed_task_files_are_loaded_again(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    run(shell, SUBTRACT, "cell-2")
    assert outcomes(live, task) == {"adds": "failed"}

    stat = task.stat()
    write_task(
        tmp_path,
        ADD_CELLS,
        lambda nb: [SimpleTest("subtracts", nb, "add", -1, function_args=[1, 2])],
    )
    os.utime(task, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    run(shell, SUBTRACT, "cell-2")
    assert outcomes(live, task) == {"subtracts": "passed"}


def test_a_broken_task_file_keeps_the_tests_and_is_reported_once(
    shell: InteractiveShell,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension_log: list[logging.LogRecord],
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    text = task.read_text(encoding="utf-8")
    task.write_text("{ half written", encoding="utf-8")
    assert_silent(run(shell, ADD, "cell-2"))
    assert_silent(run(shell, SUBTRACT, "cell-2"))
    assert outcomes(live, task) == {"adds": "failed"}
    warnings = [r.getMessage() for r in extension_log if r.levelno >= logging.WARNING]
    assert warnings == [f"Can't load the changed task {task}"]

    task.write_text(text, encoding="utf-8")
    run(shell, ADD, "cell-2")
    assert outcomes(live, task) == {"adds": "passed"}


# --- pausing and interrupting ---


def test_pyics_off_pauses_live_runs(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    run(shell, ADD, "cell-2")
    first_run = live_results(live).run

    assert_silent(run(shell, "%pyics off", "cell-9"))
    run(shell, SUBTRACT, "cell-2")
    assert live_results(live).run == first_run
    assert outcomes(live, task) == {"adds": "passed"}

    assert_silent(run(shell, "%pyics on", "cell-9"))
    run(shell, SUBTRACT, "cell-2")
    assert outcomes(live, task) == {"adds": "failed"}

    # the TUI pauses them, too
    write_json_atomic(channel_dir(notebook) / CONTROL_FILE, {"paused": True})
    run_before = live_results(live).run
    run(shell, ADD, "cell-2")
    assert live_results(live).run == run_before


def test_an_interrupt_in_a_tested_function_stops_the_run(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tests(notebook: Path) -> list[Test]:
        return [*adds(notebook), SimpleTest("adds again", notebook, "add", 0)]

    notebook, task = write_task(tmp_path, ADD_CELLS, tests)
    live = load(shell, notebook, monkeypatch)
    captured = run(shell, "def add(a=0, b=0):\n    raise KeyboardInterrupt\n", "c-2")
    assert_silent(captured)
    results = calls(live, task)
    assert list(results) == ["adds"]
    assert results["adds"].outcome == "failed"
    assert "interrupted" in results["adds"].message_text.plain

    # the shell goes on as before
    captured = run(shell, "x = 41 + 1\nprint(x)", "cell-3")
    assert captured.stdout == "42\n"
    run(shell, ADD, "cell-2")
    assert outcomes(live, task) == {"adds": "passed", "adds again": "failed"}


def test_interrupting_an_endless_loop_in_a_tested_function_stops_the_run(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    # an endless loop, but for a deadline, so that a broken test can't hang
    loop = (
        "import threading, time\n"
        "looping = threading.Event()\n"
        "def add(a, b):\n"
        "    looping.set()\n"
        "    deadline = time.monotonic() + 30\n"
        "    while time.monotonic() < deadline:\n"
        "        pass\n"
    )

    def interrupt() -> None:
        # once the tested function loops, as the frontend's interrupt button would
        while "looping" not in shell.user_ns:
            time.sleep(0.01)
        if shell.user_ns["looping"].wait(20):
            _thread.interrupt_main()

    started = time.monotonic()
    interrupter = threading.Thread(target=interrupt, daemon=True)
    interrupter.start()
    try:
        captured = run(shell, loop, "cell-2")
    except KeyboardInterrupt:
        pytest.fail("the interrupt didn't stop the live run")
    finally:
        interrupter.join()
    assert time.monotonic() - started < 20
    assert_silent(captured)
    result = calls(live, task)["adds"]
    assert result.outcome == "failed"
    assert "interrupted" in result.message_text.plain
    # the kernel goes on (with a function that ends)
    captured = run(shell, ADD + "print('still here')", "cell-2")
    assert captured.stdout == "still here\n"
    assert outcomes(live, task) == {"adds": "passed"}


def test_errors_of_the_extension_never_reach_the_notebook(
    shell: InteractiveShell,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension_log: list[logging.LogRecord],
) -> None:
    notebook, _ = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    assert live.channel is not None
    monkeypatch.setattr(live.channel, "write_results", fail)
    assert_silent(run(shell, ADD, "cell-2"))
    assert [r.getMessage() for r in extension_log if r.levelno >= logging.WARNING] == [
        "The live run failed"
    ]
    assert run(shell, "print(1)", "cell-3").stdout == "1\n"


# --- loading ---


def test_loading_twice_registers_once_and_unloading_cleans_up(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, _ = write_task(tmp_path, ADD_CELLS, adds)
    live = load(shell, notebook, monkeypatch)
    pyics.load_ipython_extension(shell)
    assert extension.loaded(shell) is live
    run(shell, "%load_ext pyics", "cell-9")
    handlers = shell.events.callbacks["post_run_cell"]
    assert handlers.count(live.post_run_cell) == 1
    assert "pyics" in shell.magics_manager.magics["line"]
    assert live.channel is not None
    directory = live.channel.directory
    assert any(directory.iterdir())

    pyics.unload_ipython_extension(shell)
    assert extension.loaded(shell) is None
    assert live.post_run_cell not in shell.events.callbacks["post_run_cell"]
    assert "pyics" not in shell.magics_manager.magics["line"]
    assert list(directory.iterdir()) == []
    assert logging.getLogger("pyics").propagate


def test_importing_pyics_does_not_import_ipython() -> None:
    import subprocess

    code = "import sys, pyics; print('IPython' in sys.modules)"
    output = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "False"


# --- the cells the kernel ran ---


def _cell(index: int, source: str, cell_id: str | None = None) -> Cell:
    return Cell(index, "code", cell_id or f"cell-{index + 1}", (), source)


def test_cells_that_are_not_saved_follow_the_cell_that_ran_before_them() -> None:
    nb = {
        "nbformat": 4,
        "cells": [
            {"cell_type": "code", "id": "a", "source": "a = 1", "metadata": {}},
            {"cell_type": "code", "id": "b", "source": "b = 1", "metadata": {}},
            {"cell_type": "code", "id": "c", "source": "c = 1", "metadata": {}},
        ],
    }
    log = CellLog()
    log.record("new-1", "n1 = 1")  # before any saved cell ran: at the start
    log.record("a", "a = 2")
    log.record("new-2", "n2 = 1")
    log.record("c", "c = 2")
    log.record("new-3", "n3 = 1")
    log.record("b", "b = 2")
    log.record("new-4", "n4 = 1")
    log.record("new-3", "n3 = 2")  # ran again after b: follows b now
    log.record(None, "ignored = 1")
    sources = [cell.source for cell in log.code_cells(nb)]
    assert sources == [
        "n1 = 1",
        "a = 2",
        "n2 = 1",
        "b = 2",
        "n4 = 1",
        "n3 = 2",
        "c = 2",
    ]
    numbers = [cell.number for cell in log.code_cells(nb)]
    assert numbers == [1, 1, 2, 2, 3, 4, 3]


def test_cells_of_a_frontends_own_ids_are_matched_by_their_source() -> None:
    saved = [_cell(0, "x = 1\n"), _cell(1, "def f():\n    return 1\n")]
    log = CellLog()
    log.record("vscode-notebook-cell:/nb.ipynb#W1", "def f():\n    return 1")
    assert log.match(saved) == {"vscode-notebook-cell:/nb.ipynb#W1": saved[1]}
    # once matched, it stays that cell when it changes
    log.record("vscode-notebook-cell:/nb.ipynb#W1", "def f():\n    return 2\n")
    assert log.match(saved) == {"vscode-notebook-cell:/nb.ipynb#W1": saved[1]}
    log.record("vscode-notebook-cell:/nb.ipynb#W9", "y = 2")
    assert "vscode-notebook-cell:/nb.ipynb#W9" not in log.match(saved)
