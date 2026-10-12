"""
The channel through which a notebook's kernel passes its live results to the TUI
(``pyics_jupyter.channel``): one file per kernel, heartbeats, cleaning up after crashed
kernels, files that are ignored, and atomic writes.
"""

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from live_helpers import FakeClock

from pyics.tests.results import TestResult
from pyics_jupyter import channel
from pyics_jupyter.channel import (
    KernelChannel,
    KernelFile,
    channel_dir,
    read_json,
    read_kernels,
    same_file,
    write_json,
)
from pyics_jupyter.overlay import LiveOverlay

PASSED = TestResult("::adds", "call", "passed", 0.01, index=0)
FAILED = TestResult("::adds", "call", "failed", 0.01, "adds failed: 0 == 3", index=0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notebook(tmp_path: Path) -> Path:
    path = tmp_path / "add.ipynb"
    path.write_text("{}")
    return path


@pytest.fixture
def kernel(notebook: Path, clock: FakeClock) -> Iterator[Callable[[], KernelChannel]]:
    """Starts kernels for ``notebook`` on the fake clock, without a heartbeat thread.
    Their files are removed after the test."""
    started: list[KernelChannel] = []

    def start() -> KernelChannel:
        started.append(KernelChannel(notebook, clock=clock))
        started[-1].open(heartbeat=False)
        return started[-1]

    yield start
    for k in started:
        k.close()


def test_a_kernel_writes_one_file_next_to_the_notebook(
    notebook: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    k = kernel()
    directory = notebook.parent / ".pyics/add.ipynb"
    assert k.path.parent == directory == channel_dir(notebook)
    assert (directory.parent / ".gitignore").read_text() == "*\n"
    assert [p.name for p in directory.iterdir()] == [k.path.name]
    assert read_kernels(directory, now=clock()) == [KernelFile(k.latest.kernel_id)]

    k.write({notebook.parent / "add.pyicstask.json": [PASSED]}, 2)
    task = same_file(notebook.parent / "add.pyicstask.json")
    assert read_kernels(directory, now=clock()) == [
        KernelFile(k.latest.kernel_id, clock(), 2, {task: (PASSED,)})
    ]
    assert read_json(k.path)["version"] == channel.VERSION

    k.close()
    assert list(directory.iterdir()) == []
    assert read_kernels(directory, now=clock()) == []


def test_a_kernel_without_a_heartbeat_for_10_s_is_gone(
    notebook: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    k = kernel()
    directory = channel_dir(notebook)
    clock.advance(9)
    assert len(read_kernels(directory, now=clock())) == 1
    k.beat()
    clock.advance(9)
    assert len(read_kernels(directory, now=clock())) == 1
    clock.advance(2)
    assert read_kernels(directory, now=clock()) == []
    # a heartbeat brings it back, and so does a file that was removed meanwhile
    os.remove(k.path)
    k.beat()
    assert len(read_kernels(directory, now=clock())) == 1


def test_the_heartbeat_thread_beats_until_the_kernel_closes(notebook: Path) -> None:
    k = KernelChannel(notebook, interval=0.01)
    k.open()
    try:
        os.utime(k.path, (0, 0))
        deadline = time.monotonic() + 10
        while os.stat(k.path).st_mtime == 0:
            assert time.monotonic() < deadline, "no heartbeat"
            time.sleep(0.01)
    finally:
        k.close()
    time.sleep(0.05)
    assert not k.path.exists()


def test_stale_files_are_removed_after_an_hour(
    notebook: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    directory = channel_dir(notebook)
    directory.mkdir(parents=True)
    crashed, temp, recent = (
        directory / "kernel-crashed.json",
        directory / ".kernel-crashed.json.123.tmp",
        directory / "kernel-recent.json",
    )
    for path, age in ((crashed, 3601), (temp, 3601), (recent, 3500)):
        write_json(path, KernelFile(path.stem).to_json())
        os.utime(path, (clock() - age, clock() - age))

    k = kernel()
    assert {p.name for p in directory.iterdir()} == {"kernel-recent.json", k.path.name}
    # the recent one is kept, but it is gone
    assert read_kernels(directory, now=clock()) == [KernelFile(k.latest.kernel_id)]


@pytest.mark.parametrize(
    "contents",
    [
        "{ half written",
        "[]",
        json.dumps({**KernelFile("other").to_json(), "version": channel.VERSION + 1}),
        json.dumps({**KernelFile("other").to_json(), "tasks": {"t": [{"x": 1}]}}),
        json.dumps({**KernelFile("other").to_json(), "kernel_id": 3}),
    ],
)
def test_files_that_cannot_be_read_are_ignored(
    notebook: Path,
    kernel: Callable[[], KernelChannel],
    clock: FakeClock,
    contents: str,
) -> None:
    k = kernel()
    (k.path.parent / "kernel-other.json").write_text(contents)
    assert read_kernels(k.path.parent, now=clock()) == [k.latest]


def test_the_newest_live_run_of_all_kernels_wins(
    notebook: Path, kernel: Callable[[], KernelChannel], clock: FakeClock
) -> None:
    task = notebook.parent / "add.pyicstask.json"
    overlay = LiveOverlay(task, [notebook], clock=clock, interval=0)
    first, second = kernel(), kernel()
    second.write({task: [FAILED]}, None)
    clock.advance(1)
    first.write({task: [PASSED]}, 3)
    second.write({notebook.parent / "other.pyicstask.json": [FAILED]}, 4)
    saved = [TestResult("::adds", "call", "failed", 0.01, index=0)]
    assert overlay.poll()
    assert overlay.apply(saved, clock() - 60).results == [
        TestResult("::adds (live)", "call", "passed", 0.01, index=0)
    ]
    assert not overlay.poll()


def _payload(i: int) -> list[TestResult]:
    return [PASSED] * (i % 7) + [FAILED] * (i % 11)


def test_readers_never_see_a_partly_written_file(
    notebook: Path, kernel: Callable[[], KernelChannel]
) -> None:
    k = KernelChannel(notebook)
    k.open(heartbeat=False)
    task = notebook.parent / "add.pyicstask.json"
    errors: list[BaseException] = []
    done = threading.Event()

    def write() -> None:
        try:
            for i in range(1, 300):
                k.write({task: _payload(i)}, i)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    writer = threading.Thread(target=write)
    writer.start()
    seen: list[int] = []
    try:
        while not done.is_set() or len(seen) < 50:
            (found,) = read_kernels(k.path.parent, now=time.time())
            if found.cell is not None:
                assert found.tasks[same_file(task)] == tuple(_payload(found.cell))
                seen.append(found.cell)
    finally:
        writer.join()
        k.close()
    assert not errors
    # never an older file after a newer one
    assert seen == sorted(seen)
    assert list(k.path.parent.iterdir()) == []


def test_permission_errors_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As on Windows, where a file can't be replaced while another process has it
    open, nor opened while it is being replaced."""
    path = tmp_path / "kernel-1.json"
    write_json(path, {"run": 1})
    failures = {"replace": 3, "read": 3}
    replace, read_bytes = os.replace, Path.read_bytes

    def flaky_replace(src: Any, dst: Any) -> None:
        if failures["replace"]:
            failures["replace"] -= 1
            raise PermissionError(13, "Access is denied", str(dst))
        replace(src, dst)

    def flaky_read(self: Path) -> bytes:
        if failures["read"]:
            failures["read"] -= 1
            raise PermissionError(13, "Access is denied", str(self))
        return read_bytes(self)

    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(Path, "read_bytes", flaky_read)
    write_json(path, {"run": 2})
    assert read_json(path) == {"run": 2}
    assert failures == {"replace": 0, "read": 0}

    # given up on after a while, leaving the file as it was
    monkeypatch.setattr(channel, "RETRY_FOR", 0.01)
    failures["replace"] = 1000
    with pytest.raises(PermissionError):
        write_json(path, {"run": 3})
    failures["read"] = 1000
    assert read_json(path, 0.01) is None
    monkeypatch.undo()
    assert [p.name for p in tmp_path.iterdir()] == ["kernel-1.json"]
    assert read_json(path) == {"run": 2}
