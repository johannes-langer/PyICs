"""
End to end: a real Jupyter kernel with the pyics extension (``--ext=pyics``, as the
"Python (pyics)" kernel starts it), driven through ``jupyter_client`` like a frontend
drives it, for a notebook of a course: live results in the notebook's channel, cells
without any output of pyics, interrupting a tested endless loop, and the kernel's
files removed when it shuts down.
"""

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from jupyter_client import BlockingKernelClient, KernelManager
from jupyter_client.kernelspec import KernelSpecManager
from notebook_helpers import markdown, write_notebook

from pyics.data import Course, Task
from pyics.notebook.channel import LiveResults, channel_dir, read_json
from pyics.tests.tests import PrintTest, SimpleTest

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
    """Starts a kernel with the pyics extension for a notebook, as Jupyter's server
    does (in its directory, with its path as ``JPY_SESSION_NAME``). Skips the test if
    no kernel can be started. Kernels still running afterwards are shut down."""
    started: list[Kernel] = []

    def start(notebook: Path) -> Kernel:
        spec = tmp_path / "kernels/pyics-test"
        spec.mkdir(parents=True)
        argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
        kernel_json = {
            "argv": [*argv, "--ext=pyics"],
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
    """The output the request ``msg_id`` shows in the notebook, once it finished
    (its reply's status must be "ok")."""
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


def live_results(notebook: Path) -> LiveResults:
    (path,) = channel_dir(notebook).glob("results-*.json")
    data = read_json(path)
    assert data is not None
    return LiveResults.from_json(data, path.stem.removeprefix("results-"))


def outcomes(notebook: Path, task: Path) -> dict[str, tuple[str, str | None]]:
    results = live_results(notebook).for_task(task)
    assert results is not None
    return {
        r.nodeid.removeprefix("::"): (r.outcome, r.message)
        for r in results
        if r.when == "call"
    }


def test_a_kernel_runs_the_tests_live_and_silently(
    tmp_path: Path, start_kernel: Callable[[Path], Kernel]
) -> None:
    notebook = write_notebook(
        tmp_path / "add.ipynb",
        [markdown("# Adding"), "def add(a, b):\n    return 0\n", "print(add(1, 2))"],
    )
    solution = write_notebook(tmp_path / ".solutions/add.ipynb", [])
    tests = [
        SimpleTest("adds", notebook, "add", 3, function_args=[1, 2]),
        PrintTest("prints", notebook, "3\n"),
    ]
    task = Task.create_task_json(
        tmp_path / ".tasks/add", "Add", "Implement add.", notebook, solution, tests
    )
    Course.create_course_manifest(tmp_path / "course", [task], name="Adding")
    km, kc = start_kernel(notebook)

    # the kernel announced itself when it started
    (kernel_file,) = channel_dir(notebook).glob("kernel-*.json")
    assert read_json(kernel_file)["task_files"] == [task.as_posix()]
    assert execute(kc, f"print(__session__ == {str(notebook)!r})", "cell-9") == [
        {"msg_type": "stream", "name": "stdout", "text": "True\n"}
    ]

    noisy = (
        "import sys\n"
        "def add(a, b):\n"
        "    print('adding')\n"
        "    print('careful', file=sys.stderr)\n"
        "    return a + b\n"
    )
    assert execute(kc, noisy, "cell-2") == []
    results = live_results(notebook)
    assert (results.cell.id, results.cell.number) == ("cell-2", 2)
    assert outcomes(notebook, task) == {
        "adds": ("passed", None),
        "prints": ("skipped", "Skipped: runs when the notebook is saved"),
    }
    assert execute(kc, "def add(a, b):\n    return a - b\n", "cell-2") == []
    assert outcomes(notebook, task)["adds"] == ("failed", "adds failed: -1 == 3")

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
    adds = outcomes(notebook, task)["adds"]
    assert adds[0] == "failed" and "interrupted" in (adds[1] or "")

    # the kernel goes on
    fixed = "def add(a, b):\n    return a + b\nprint('still here')\n"
    assert execute(kc, fixed, "cell-2") == [
        {"msg_type": "stream", "name": "stdout", "text": "still here\n"}
    ]
    assert outcomes(notebook, task)["adds"] == ("passed", None)

    # a kernel that shuts down cleanly takes its files with it
    km.shutdown_kernel(now=False)
    assert list(channel_dir(notebook).iterdir()) == []
