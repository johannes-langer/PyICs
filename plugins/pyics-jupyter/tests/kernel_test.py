"""
End to end: a real Jupyter kernel with the extension (``--ext=pyics_jupyter``, as the
"Python (pyics)" kernel starts it), driven through ``jupyter_client`` like a frontend
drives it, next to the TUI's ticking ``Course``: live results without any output in
the notebook, progress only from the saved notebook, interrupting a tested endless
loop, and a killed kernel.
"""

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from jupyter_client import BlockingKernelClient, KernelManager
from jupyter_client.kernelspec import KernelSpecManager
from live_helpers import ADD, SOLUTION, SUBTRACT, TEMPLATE, text, write_notebook

from pyics.data import Course, Task
from pyics.tests.tests import PrintTest, SimpleTest
from pyics_jupyter.overlay import NO_KERNEL, SOLVED_LIVE, LiveOverlay

# generous, for slow CI machines
STARTUP_TIMEOUT = 120.0
TIMEOUT = 60.0
# the messages of a cell that show up as its output
OUTPUT_MESSAGES = {
    "stream",
    "display_data",
    "update_display_data",
    "execute_result",
    "error",
    "clear_output",
}

type Kernel = tuple[KernelManager, BlockingKernelClient]


@pytest.fixture
def start_kernel(tmp_path: Path) -> Iterator[Callable[[Path], Kernel]]:
    """Starts a kernel with the extension for a notebook, as Jupyter's server does (in
    its directory, with its path as ``JPY_SESSION_NAME``). Skips the test if no kernel
    can be started. Kernels still running afterwards are shut down."""
    started: list[Kernel] = []

    def start(notebook: Path) -> Kernel:
        spec = tmp_path / "kernels/pyics-test"
        spec.mkdir(parents=True)
        argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
        kernel_json = {
            "argv": [*argv, "--ext=pyics_jupyter"],
            "display_name": "Python (pyics, test)",
            "language": "python",
        }
        (spec / "kernel.json").write_text(json.dumps(kernel_json), encoding="utf-8")
        specs = KernelSpecManager(kernel_dirs=[str(spec.parent)])
        km = KernelManager(kernel_name="pyics-test", kernel_spec_manager=specs)
        # as outside of pytest: ipykernel doesn't forward its process's output then
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        env["JPY_SESSION_NAME"] = str(notebook)
        env["IPYTHONDIR"] = str(tmp_path / "ipython")
        try:
            km.start_kernel(cwd=str(notebook.parent), env=env)
            kc = km.client()
            kc.start_channels()
            started.append((km, kc))
            kc.wait_for_ready(timeout=STARTUP_TIMEOUT)
        except Exception as err:  # noqa: BLE001
            pytest.skip(f"Can't start a Jupyter kernel: {err!r}")
        return km, kc

    yield start
    for km, kc in started:
        kc.stop_channels()
        if km.has_kernel and km.is_alive():
            km.shutdown_kernel(now=True)


def send(kc: BlockingKernelClient, code: str, cell_id: str) -> str:
    """Execute ``code`` as the cell ``cell_id`` of a notebook, as JupyterLab does.
    Returns the request's id."""
    content = {
        "code": code,
        "silent": False,
        "store_history": True,
        "user_expressions": {},
        "allow_stdin": False,
        "stop_on_error": True,
    }
    msg = kc.session.msg("execute_request", content, metadata={"cellId": cell_id})
    kc.shell_channel.send(msg)
    return msg["header"]["msg_id"]


def outputs(kc: BlockingKernelClient, msg_id: str) -> list[dict[str, Any]]:
    """The output the request ``msg_id`` shows in the notebook, once it finished (its
    reply's status must be "ok")."""
    shown = []
    deadline = time.monotonic() + TIMEOUT
    while True:
        msg = kc.get_iopub_msg(timeout=max(0.1, deadline - time.monotonic()))
        if msg["parent_header"].get("msg_id") != msg_id:
            continue
        if msg["msg_type"] == "status":
            if msg["content"]["execution_state"] == "idle":
                break
        elif msg["msg_type"] in OUTPUT_MESSAGES:
            shown.append({"msg_type": msg["msg_type"], **msg["content"]})
    while True:
        reply = kc.get_shell_msg(timeout=TIMEOUT)
        if reply["parent_header"].get("msg_id") == msg_id:
            assert reply["content"]["status"] == "ok", reply["content"]
            return shown


def execute(kc: BlockingKernelClient, code: str, cell_id: str) -> list[dict[str, Any]]:
    """Execute ``code`` as the cell ``cell_id``, and return what it shows."""
    return outputs(kc, send(kc, code, cell_id))


def test_the_tui_shows_the_live_results_of_a_real_kernel(
    tmp_path: Path,
    start_kernel: Callable[[Path], Kernel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # where the TUI's pytest runs write their cache
    monkeypatch.chdir(tmp_path)
    # a killed kernel is gone sooner than after 10 s, with a margin for slow machines
    monkeypatch.setattr(
        "pyics_jupyter.plugin.LiveOverlay", partial(LiveOverlay, timeout=6.0)
    )
    notebook = write_notebook(tmp_path / "add.ipynb", TEMPLATE)
    solution = write_notebook(tmp_path / ".solutions/add.ipynb", SOLUTION)
    tests = [
        SimpleTest("adds", notebook, "add", 3, function_args=[1, 2]),
        PrintTest("prints", notebook, "Adding\n"),
    ]
    task = Task.create_task_json(
        tmp_path / ".tasks/add", "Add", "Implement add.", notebook, solution, tests
    )
    manifest = Course.create_course_manifest(tmp_path / "course", [task], name="Adding")
    km, kc = start_kernel(notebook)
    threads = set(threading.enumerate())
    course = Course.load_course_manifest(manifest)

    def tick_until(*texts: str, progress: float | None = None) -> str:
        """Tick the course, as the TUI does, until its status shows ``texts`` (and its
        progress is ``progress``, if given)."""
        deadline = time.monotonic() + TIMEOUT
        while True:
            course.tick()
            status = text(course.status)
            if all(t in status for t in texts) and progress in (None, course.progress):
                return status
            assert time.monotonic() < deadline, f"timed out, showing:\n{status}"
            time.sleep(0.05)

    try:
        # the kernel is connected, but hasn't run a cell yet
        status = tick_until("1 failed, 1 passed")
        assert NO_KERNEL not in status and "Live results" not in status

        # defining the function shows its live pass, without any output in the cell
        noisy = "def add(a, b):\n    print('adding')\n    return a + b\n"
        assert execute(kc, noisy, "cell-2") == []
        tick_until("Live results from your notebook kernel (cell 2,", SOLVED_LIVE)
        assert course.progress == 0

        # redefining it wrongly turns it red
        assert execute(kc, SUBTRACT, "cell-2") == []
        tick_until("FAILED ::adds (live) - adds failed: -1 == 3")

        # only saving the notebook counts
        execute(kc, ADD, "cell-2")
        tick_until(SOLVED_LIVE)
        assert course.progress == 0
        write_notebook(notebook, SOLUTION)
        now = time.time()  # after the live run, even where file times are coarse
        os.utime(notebook, (now, now))
        status = tick_until("2 passed", progress=1)
        assert "Live results" not in status and SOLVED_LIVE not in status

        # interrupting the kernel stops a tested endless loop
        looping = tmp_path / "looping"
        endless = (
            "import pathlib, time\n"
            "def add(a, b):\n"
            f"    pathlib.Path({str(looping)!r}).touch()\n"
            "    deadline = time.monotonic() + 60\n"
            "    while time.monotonic() < deadline:\n"
            "        pass\n"
        )
        msg_id = send(kc, endless, "cell-2")
        deadline = time.monotonic() + TIMEOUT
        while not looping.exists():
            assert time.monotonic() < deadline, "the tested function never ran"
            time.sleep(0.05)
        started = time.monotonic()
        km.interrupt_kernel()
        assert outputs(kc, msg_id) == []
        assert time.monotonic() - started < 30
        tick_until("FAILED ::adds (live) - adds failed: the kernel was interrupted")

        # ... and the kernel goes on
        fixed = ADD + "print('still here')\n"
        assert execute(kc, fixed, "cell-2") == [
            {"msg_type": "stream", "name": "stdout", "text": "still here\n"}
        ]
        tick_until("Live results from your notebook kernel", "2 passed")

        # killing it drops the live results once its heartbeat is missed
        execute(kc, SUBTRACT, "cell-2")
        tick_until("FAILED ::adds (live)")
        km.shutdown_kernel(now=True)
        status = tick_until(NO_KERNEL)
        assert "2 passed" in status and "(live)" not in status
        assert course.progress == 1
    finally:
        course.stop_progress_check()
        for thread in set(threading.enumerate()) - threads:
            thread.join(TIMEOUT)
        # the course's own pytest runs remove this when they finish
        os.environ.setdefault("PYTEST_CURRENT_TEST", "")
