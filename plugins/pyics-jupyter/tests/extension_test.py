"""
The IPython extension (``pyics_jupyter.extension``), driven through a real
``InteractiveShell``: finding the notebook's tasks, live results written to the
channel, silent runs, skipped output and structure tests, interrupting a run, and
errors that never reach the notebook.
"""

import _thread
import atexit
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import CapturedIO, capture_output
from live_helpers import ADD, SUBTRACT, adds, calls, write_task
from traitlets.config import Config

import pyics_jupyter
from pyics.tests.tests import PrintTest, SimpleTest, StructureTest, Test
from pyics_jupyter import extension
from pyics_jupyter.channel import channel_dir
from pyics_jupyter.extension import LiveRuns, find_tasks, notebook_path


@pytest.fixture
def shell(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[InteractiveShell]:
    """IPython's shell, as a kernel runs it, without the user's history or profile. The
    extension, the shell and what the shell changes are cleaned up afterwards."""
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path_factory.mktemp("ipython")))
    main = sys.modules.get("__main__")
    config = Config()
    config.HistoryManager.hist_file = ":memory:"
    ip = InteractiveShell.instance(config=config)
    try:
        yield ip
    finally:
        pyics_jupyter.unload_ipython_extension(ip)
        atexit.unregister(ip.atexit_operations)
        InteractiveShell.clear_instance()
        sys.modules.pop("current_solution", None)
        if main is not None:
            sys.modules["__main__"] = main


def load(
    ip: InteractiveShell, notebook: Path | None, monkeypatch: pytest.MonkeyPatch
) -> LiveRuns:
    """Load the extension into ``ip`` as a kernel for ``notebook`` (if given) does, in
    the notebook's directory."""
    if notebook is not None:
        ip.user_ns["__session__"] = str(notebook)
        monkeypatch.chdir(notebook.parent)
    pyics_jupyter.load_ipython_extension(ip)
    return extension._loaded[ip]


def run(ip: InteractiveShell, source: str, cell_id: str | None = None) -> CapturedIO:
    """Run ``source`` as a frontend runs a cell, and return what the cell shows."""
    with capture_output() as captured:
        ip.run_cell(source, store_history=True, cell_id=cell_id)
    return captured


def assert_silent(captured: CapturedIO) -> None:
    assert (captured.stdout, captured.stderr, captured.outputs) == ("", "", [])


def outcomes(notebook: Path, task: Path) -> dict[str, str]:
    return {name: r.outcome for name, r in calls(notebook, task).items()}


def test_running_a_cell_writes_live_results(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path)
    live = load(shell, notebook, monkeypatch)
    assert list(live.tasks) == [task]
    assert live.channel is not None and live.channel.directory == channel_dir(notebook)

    assert_silent(run(shell, ADD, "cell-2"))
    assert outcomes(notebook, task) == {"adds": "passed"}
    assert live.channel.latest.cell == 2

    run(shell, "1 / 0", "cell-9")
    assert_silent(run(shell, SUBTRACT, "new-cell"))
    result = calls(notebook, task)["adds"]
    assert (result.outcome, result.message) == ("failed", "adds failed: -1 == 3")
    assert live.channel.latest.cell is None  # not in the saved notebook
    # %debug still shows the student's error, rather than the test's
    assert isinstance(sys.last_value, ZeroDivisionError)
    assert "current_solution" not in sys.modules


def test_nothing_of_a_live_run_reaches_the_cell_output(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path)
    load(shell, notebook, monkeypatch)
    stdout, stderr, display_pub = sys.stdout, sys.stderr, shell.display_pub

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
    assert (captured.stdout, captured.stderr, captured.outputs) == (
        "the cell itself\n",
        "",
        [],
    )
    assert outcomes(notebook, task) == {"adds": "passed"}
    assert (sys.stdout, sys.stderr, shell.display_pub) == (stdout, stderr, display_pub)

    # input() doesn't wait for an answer
    assert_silent(run(shell, "def add(a, b):\n    return int(input('a? ')) + b\n"))
    assert "EOF" in calls(notebook, task)["adds"].message_text.plain


def test_output_and_structure_tests_run_when_the_notebook_is_saved(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tests(notebook: Path) -> list[Test]:
        return [
            PrintTest("prints", notebook, "Adding\n"),
            StructureTest("no loops", notebook, {"concept": "loop", "max": 0}),
            *adds(notebook),
        ]

    notebook, task = write_task(tmp_path, tests)
    load(shell, notebook, monkeypatch)
    assert_silent(run(shell, ADD, "cell-2"))
    results = calls(notebook, task)
    assert {name: r.outcome for name, r in results.items()} == {
        "prints": "skipped",
        "no loops": "skipped",
        "adds": "passed",
    }
    assert results["prints"].message == "Skipped: runs when the notebook is saved"


def test_the_notebook_and_its_tasks_are_found(tmp_path: Path) -> None:
    root = tmp_path / "course"
    notebook, task = write_task(root)
    nested = root / "x/y/z/add.ipynb"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")
    assert find_tasks(notebook) == {task: []}
    assert find_tasks(nested) == {}  # no course lists it
    assert find_tasks(tmp_path / "elsewhere.ipynb") == {}

    assert notebook_path({}, root) is None
    assert notebook_path({"__session__": "console-1"}, root) is None
    assert notebook_path({"__session__": str(notebook)}, tmp_path) == notebook
    # relative to the server's root, while the kernel runs in the notebook's directory
    assert notebook_path({"__session__": "course/add.ipynb"}, root) == notebook
    vs_code = {"__session__": "x.ipynb", "__vsc_ipynb_file__": str(notebook)}
    assert notebook_path(vs_code, tmp_path) == notebook


def test_vs_codes_notebook_variable_is_looked_for_at_each_cell(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, task = write_task(tmp_path)
    monkeypatch.chdir(tmp_path)
    live = load(shell, None, monkeypatch)
    assert_silent(run(shell, ADD))
    assert live.notebook is None and live.channel is None

    shell.user_ns["__vsc_ipynb_file__"] = str(notebook)
    assert_silent(run(shell, ADD))
    assert outcomes(notebook, task) == {"adds": "passed"}


def test_without_a_task_a_single_warning_says_so(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, _ = write_task(tmp_path, course=False)
    live = load(shell, notebook, monkeypatch)
    captured = run(shell, ADD, "cell-2")
    assert captured.stdout == "" and captured.outputs == []
    assert captured.stderr == (
        "pyics: no task found for add.ipynb (no course near it lists it as an "
        "exercise file), so no tests run when its cells run.\n"
    )
    assert live.channel is None and not channel_dir(notebook).exists()
    assert_silent(run(shell, ADD, "cell-2"))


def test_interrupting_a_tested_endless_loop_stops_the_live_run(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tests(notebook: Path) -> list[Test]:
        return [*adds(notebook), SimpleTest("adds again", notebook, "add", 0)]

    notebook, task = write_task(tmp_path, tests)
    load(shell, notebook, monkeypatch)
    # an endless loop, but for a deadline, so that a broken test can't hang
    loop = (
        "import threading, time\n"
        "looping = threading.Event()\n"
        "def add(a=0, b=0):\n"
        "    looping.set()\n"
        "    deadline = time.monotonic() + 30\n"
        "    while time.monotonic() < deadline:\n"
        "        pass\n"
    )

    def interrupt() -> None:
        # once the tested function loops, as the frontend's interrupt button does
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
    results = calls(notebook, task)
    assert list(results) == ["adds"]
    assert results["adds"].outcome == "failed"
    assert "interrupted" in results["adds"].message_text.plain

    # the shell goes on
    assert run(shell, ADD + "print('still here')", "cell-2").stdout == "still here\n"
    assert outcomes(notebook, task) == {"adds": "passed", "adds again": "failed"}


def test_errors_of_the_extension_never_reach_the_notebook(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, _ = write_task(tmp_path)
    live = load(shell, notebook, monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(live.channel, "write", fail)
    assert_silent(run(shell, ADD, "cell-2"))
    assert run(shell, "print(1)", "cell-3").stdout == "1\n"


def test_unloading_stops_live_runs_and_removes_the_kernel_file(
    shell: InteractiveShell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notebook, _ = write_task(tmp_path)
    live = load(shell, notebook, monkeypatch)
    run(shell, "%load_ext pyics_jupyter", "cell-9")
    assert shell.events.callbacks["post_run_cell"].count(live.post_run_cell) == 1
    assert any(channel_dir(notebook).iterdir())

    assert_silent(run(shell, "%unload_ext pyics_jupyter", "cell-9"))
    assert live.post_run_cell not in shell.events.callbacks["post_run_cell"]
    assert list(channel_dir(notebook).iterdir()) == []
    assert_silent(run(shell, ADD, "cell-2"))
    assert list(channel_dir(notebook).iterdir()) == []
