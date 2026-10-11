"""
The channel through which a notebook's kernel passes live results to the TUI
(``pyics.notebook.channel``): heartbeats, choosing the connected kernel, protocol
versions, cleaning up after crashed kernels, and atomic writes.
"""

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pyics.notebook import channel
from pyics.notebook.channel import (
    PROTOCOL,
    CellRef,
    ChannelReader,
    ChannelStatus,
    Control,
    KernelChannel,
    channel_dir,
    read_json,
    write_json_atomic,
)
from pyics.tests.results import TestResult

PASSED = TestResult("::adds", "call", "passed", 0.01, index=0)
FAILED = TestResult("::adds", "call", "failed", 0.01, "adds failed: 3 == 4", index=0)


class FakeClock:
    """Time that only passes when a test says so. It starts at the real time, so that it
    agrees with the times of files that aren't set by it."""

    def __init__(self) -> None:
        self.now = time.time()
        self.elapsed = 0.0

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.elapsed += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notebook(tmp_path: Path) -> Path:
    path = tmp_path / "lecture.ipynb"
    path.write_text("{}")
    return path


@pytest.fixture
def task_file(tmp_path: Path) -> Path:
    path = tmp_path / ".tasks/add.pyicstask.json"
    path.parent.mkdir()
    path.write_text("{}")
    return path


@pytest.fixture
def kernel(
    notebook: Path, clock: FakeClock, task_file: Path
) -> Iterator[Callable[..., KernelChannel]]:
    """Starts a kernel for ``notebook``, without a heartbeat thread, with the given
    ``KernelChannel`` arguments. Its files are removed after the test."""
    started: list[KernelChannel] = []

    def start(**kwargs: Any) -> KernelChannel:
        kernel = KernelChannel(notebook, clock=clock, **kwargs)
        kernel.register([task_file], heartbeat=False)
        started.append(kernel)
        return kernel

    yield start
    for kernel in started:
        kernel.close()


@pytest.fixture
def reader(notebook: Path, clock: FakeClock) -> ChannelReader:
    return ChannelReader(notebook, clock=clock, monotonic=clock.monotonic)


def _write_raw(directory: Path, name: str, data: Any) -> None:
    """Write ``data`` as JSON to ``name``, as e.g. another version of pyics would."""
    directory.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (directory / name).write_text(text, encoding="utf-8")


# --- the channel directory ---


def test_kernels_announce_themselves_next_to_the_notebook(
    notebook: Path,
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
) -> None:
    k = kernel(connection_file="kernel-1.json")
    directory = notebook.parent / ".pyics" / "lecture.ipynb"
    assert channel_dir(notebook) == directory == k.directory
    assert (notebook.parent / ".pyics/.gitignore").read_text() == "*\n"
    assert sorted(p.name for p in directory.iterdir()) == [
        f"kernel-{k.kernel_id}.alive",
        f"kernel-{k.kernel_id}.json",
    ]
    info = read_json(k.kernel_file)
    assert info["pyics_live"] == PROTOCOL
    assert info["pid"] == os.getpid()
    assert info["task_files"] == [task_file.as_posix()]

    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED
    assert state.kernel == k.info
    assert state.results is None and state.reason is None

    k.close()
    assert list(directory.iterdir()) == []
    assert reader.poll().status is ChannelStatus.NO_KERNEL


def test_without_a_channel_directory_no_kernel_is_connected(
    reader: ChannelReader,
) -> None:
    state = reader.poll()
    assert state.status is ChannelStatus.NO_KERNEL
    assert state.reason is not None and "No notebook kernel connected" in state.reason
    assert not reader.directory.exists()


def test_results_reach_the_reader(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    k = kernel()
    k.write_results(1, CellRef("abc", 3), {task_file: [FAILED]})
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED
    assert state.results is not None
    assert (state.results.run, state.results.cell) == (1, CellRef("abc", 3))
    assert state.results.for_task(task_file) == [FAILED]
    # a relative path to the same task file names the same task
    monkeypatch.chdir(task_file.parent.parent)
    assert state.results.for_task(Path(".tasks/add.pyicstask.json")) == [FAILED]
    assert state.results.for_task(tmp_path / "other.pyicstask.json") is None

    # nothing changed, so it's the same state
    assert reader.poll() is state

    k.write_results(2, None, {task_file: [PASSED]})
    state = reader.poll()
    assert state.results is not None and state.results.run == 2
    assert state.results.for_task(task_file) == [PASSED]


def test_files_are_read_again_after_a_second_even_if_they_look_unchanged(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
) -> None:
    k = kernel()
    k.write_results(1, None, {task_file: [FAILED]})
    st = os.stat(k.results_file)
    assert reader.poll().results is not None

    # same size and, as on a file system with coarse times, the same modification time
    k.write_results(2, None, {task_file: [FAILED]})
    os.utime(k.results_file, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(k.results_file).st_size == st.st_size
    state = reader.poll()
    assert state.results is not None and state.results.run == 1

    clock.advance(1.1)
    k.beat()
    state = reader.poll()
    assert state.results is not None and state.results.run == 2


# --- heartbeats ---


def test_kernels_without_a_heartbeat_for_10_s_are_gone(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
    notebook: Path,
) -> None:
    k = kernel()
    k.write_results(1, None, {task_file: [PASSED]})
    assert reader.poll().status is ChannelStatus.CONNECTED

    clock.advance(9.9)
    assert reader.poll().status is ChannelStatus.CONNECTED
    clock.advance(0.2)
    state = reader.poll()
    assert state.status is ChannelStatus.TIMED_OUT
    assert state.kernel is None and state.results is None and state.kernels == ()
    assert state.reason is not None and "stopped responding" in state.reason
    # a reader that never showed its results just doesn't see it
    fresh = ChannelReader(notebook, clock=clock, monotonic=clock.monotonic)
    assert fresh.poll().status is ChannelStatus.NO_KERNEL

    # e.g. it was busy in C code for a while
    k.beat()
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED
    assert state.results is not None and state.results.run == 1


def test_the_heartbeat_thread_beats_until_the_kernel_closes(
    notebook: Path, clock: FakeClock
) -> None:
    k = KernelChannel(notebook, clock=clock, interval=0.001)
    k.register()
    try:
        clock.advance(5.0)
        deadline = time.monotonic() + 5
        while os.stat(k.alive_file).st_mtime < clock.now - 0.001:
            assert time.monotonic() < deadline, "no heartbeat"
            time.sleep(0.001)
    finally:
        k.close()
    assert not k.alive_file.exists() and not k.kernel_file.exists()
    k.beat()  # after closing, nothing is written anymore
    assert not k.alive_file.exists()
    k.close()  # closing again does nothing


def test_a_kernel_restores_its_files_when_they_were_removed(
    kernel: Callable[..., KernelChannel], reader: ChannelReader
) -> None:
    # e.g. by a TUI, as stale, after the computer slept for hours
    k = kernel()
    k.kernel_file.unlink()
    k.alive_file.unlink()
    assert reader.poll().status is ChannelStatus.NO_KERNEL
    k.beat()
    assert reader.poll().kernel == k.info


# --- choosing the kernel ---


def test_the_kernel_whose_results_are_newest_is_connected(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
) -> None:
    first = kernel()
    clock.advance(1)
    second = kernel()
    # without results, the one started last
    state = reader.poll()
    assert state.kernel == second.info and state.results is None
    assert state.kernels == (first.info, second.info)

    clock.advance(1)
    first.write_results(1, None, {task_file: [FAILED]})
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED and state.kernel == first.info

    clock.advance(1)
    second.write_results(1, None, {task_file: [PASSED]})
    state = reader.poll()
    assert state.kernel == second.info
    assert state.results is not None and state.results.for_task(task_file) == [PASSED]

    clock.advance(1)
    first.write_results(2, None, {task_file: [FAILED]})
    assert reader.poll().kernel == first.info


def test_a_restarted_kernel_replaces_the_old_one_at_once(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
) -> None:
    old = kernel(connection_file="/run/jupyter/kernel-1.json")
    old.write_results(1, None, {task_file: [PASSED]})
    assert reader.poll().kernel == old.info

    # the frontend killed the old kernel, which still has a heartbeat, and started a
    # new one with the same connection file
    clock.advance(1)
    new = kernel(connection_file="/run/jupyter/kernel-1.json")
    state = reader.poll()
    assert state.status is ChannelStatus.RESTARTED
    assert state.kernel == new.info and state.results is None
    assert state.kernels == (new.info,)
    assert state.reason is not None and "restarted" in state.reason

    clock.advance(1)
    new.write_results(1, None, {task_file: [FAILED]})
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED and state.kernel == new.info


def test_a_new_kernel_after_a_clean_shutdown_is_a_restart(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
) -> None:
    old = kernel()
    old.write_results(1, None, {task_file: [PASSED]})
    assert reader.poll().status is ChannelStatus.CONNECTED
    old.close()
    assert reader.poll().status is ChannelStatus.NO_KERNEL
    clock.advance(1)
    new = kernel()
    state = reader.poll()
    assert state.status is ChannelStatus.RESTARTED and state.kernel == new.info


# --- other versions of pyics ---


def test_kernels_of_another_protocol_are_ignored_with_the_reason(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
) -> None:
    directory = reader.directory
    _write_raw(
        directory,
        "kernel-future.json",
        {"pyics_live": PROTOCOL + 1, "pyics_version": "9.0.0", "pid": 42},
    )
    _write_raw(directory, "kernel-future.alive", "")
    os.utime(directory / "kernel-future.alive", (clock.now, clock.now))

    state = reader.poll()
    assert state.status is ChannelStatus.INCOMPATIBLE
    assert state.kernel is None and state.kernels == ()
    assert state.reason is not None
    assert "pid 42" in state.reason and "pyics 9.0.0" in state.reason
    assert f"protocol {PROTOCOL + 1}" in state.reason
    assert "pyics install_kernel" in state.reason
    assert state.ignored == (state.reason,)

    # a kernel that this pyics understands is connected, and the other one still named
    clock.advance(1)
    k = kernel()
    k.write_results(1, None, {task_file: [PASSED]})
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED and state.kernel == k.info
    assert len(state.ignored) == 1 and "pid 42" in state.ignored[0]


def test_results_of_another_protocol_are_ignored_with_the_reason(
    kernel: Callable[..., KernelChannel], reader: ChannelReader
) -> None:
    k = kernel()
    _write_raw(
        k.directory,
        k.results_file.name,
        {"pyics_live": PROTOCOL + 1, "kernel_id": k.kernel_id, "tasks": "?"},
    )
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED
    assert state.kernel == k.info and state.results is None
    assert len(state.ignored) == 1
    assert f"live results of kernel {k.kernel_id}" in state.ignored[0]


# --- unparsable files ---


@pytest.mark.parametrize(
    "contents",
    ["", "{", '{"pyics_live": 1, "kernel_id": "', "[]", "null", '{"pyics_live": 1}'],
)
def test_unparsable_files_are_treated_as_absent(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
    contents: str,
) -> None:
    k = kernel()
    _write_raw(k.directory, k.results_file.name, contents)
    state = reader.poll()
    assert state.status is ChannelStatus.CONNECTED
    assert state.kernel == k.info and state.results is None and state.ignored == ()

    _write_raw(k.directory, k.kernel_file.name, contents)
    clock.advance(1.1)
    k.beat()  # leaves the kernel file as it is, since it exists
    assert reader.poll().status is ChannelStatus.NO_KERNEL

    # once they are written again, they are read
    k.register([task_file], heartbeat=False)
    k.write_results(1, None, {task_file: [PASSED]})
    state = reader.poll()
    assert state.results is not None and state.results.run == 1


def test_results_with_malformed_test_results_are_treated_as_absent(
    kernel: Callable[..., KernelChannel], reader: ChannelReader, task_file: Path
) -> None:
    k = kernel()
    k.write_results(1, None, {task_file: [PASSED]})
    data = read_json(k.results_file)
    data["tasks"][task_file.as_posix()][0]["outcome"] = None
    write_json_atomic(k.results_file, data)
    assert reader.poll().results is None


def test_missing_and_unparsable_files_read_as_none(tmp_path: Path) -> None:
    assert read_json(tmp_path / "missing.json") is None
    (tmp_path / "broken.json").write_bytes(b'{"a": 1')
    assert read_json(tmp_path / "broken.json") is None
    (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00")
    assert read_json(tmp_path / "binary.json") is None


# --- cleaning up ---


def test_files_of_kernels_dead_for_an_hour_are_removed(
    kernel: Callable[..., KernelChannel],
    reader: ChannelReader,
    task_file: Path,
    clock: FakeClock,
) -> None:
    dead = kernel()
    dead.write_results(1, None, {task_file: [PASSED]})
    directory = dead.directory
    reader.write_control(tracing=True)
    temp = directory / ".results-x.json.0123.tmp"
    temp.write_text("{")
    os.utime(temp, (clock.now, clock.now))

    clock.advance(3599)
    alive = kernel()
    assert reader.cleanup_stale() == []

    clock.advance(2)
    removed = reader.cleanup_stale()
    assert sorted(p.name for p in removed) == sorted(
        [
            dead.kernel_file.name,
            dead.alive_file.name,
            dead.results_file.name,
            temp.name,
        ]
    )
    # the TUI's control file and the live kernel's files are kept
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        ["control.json", alive.kernel_file.name, alive.alive_file.name]
    )
    assert (directory.parent / ".gitignore").exists()


def test_kernels_remove_the_stale_files_of_others_when_they_start(
    kernel: Callable[..., KernelChannel], clock: FakeClock
) -> None:
    dead = kernel()
    clock.advance(3601)
    new = kernel()
    assert not dead.kernel_file.exists() and not dead.alive_file.exists()
    assert new.kernel_file.exists()
    # but never its own, however long it was away
    clock.advance(7200)
    assert new.cleanup_stale() == []


def test_readers_remove_stale_files_now_and_then(
    kernel: Callable[..., KernelChannel], reader: ChannelReader, clock: FakeClock
) -> None:
    dead = kernel()
    assert reader.poll().kernel == dead.info
    clock.advance(3601)
    assert reader.poll().status is ChannelStatus.NO_KERNEL
    assert not dead.kernel_file.exists() and not dead.alive_file.exists()

    # but not at every poll
    dead = kernel()
    os.utime(dead.alive_file, (clock.now - 3601, clock.now - 3601))
    clock.advance(channel.CLEANUP_INTERVAL - 1)
    reader.poll()
    assert dead.kernel_file.exists()
    clock.advance(1)
    reader.poll()
    assert not dead.kernel_file.exists()


# --- control ---


def test_the_tui_controls_tracing_and_pausing(
    kernel: Callable[..., KernelChannel], notebook: Path
) -> None:
    reader = ChannelReader(notebook)
    assert reader.read_control() == Control()
    assert reader.write_control(tracing=True) == Control(tracing=True)
    k = kernel()
    assert k.read_control() == Control(tracing=True, paused=False)
    reader.write_control(paused=True)
    assert k.read_control() == Control(tracing=True, paused=True)
    reader.write_control(tracing=False)
    assert k.read_control() == Control(tracing=False, paused=True)

    (k.directory / "control.json").write_text('{"tracing": "yes"')
    assert k.read_control() == Control()
    (k.directory / "control.json").write_text('{"tracing": 1, "paused": true}')
    assert k.read_control() == Control(tracing=False, paused=True)


# --- atomic writes ---


def _payload(i: int) -> dict[str, Any]:
    """Contents of very different sizes, which tell whether they are complete."""
    size = (i * 7919) % 20_000
    return {"i": i, "size": size, "payload": "x" * size}


def test_readers_never_see_a_partly_written_file(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    write_json_atomic(path, _payload(0))
    errors: list[BaseException] = []
    done = threading.Event()

    def write() -> None:
        try:
            for i in range(1, 400):
                write_json_atomic(path, _payload(i))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    writer = threading.Thread(target=write)
    writer.start()
    seen: list[int] = []
    try:
        while not done.is_set() or len(seen) < 50:
            data = read_json(path)
            assert data is not None, "a partly written file was read"
            assert data["payload"] == "x" * data["size"]
            seen.append(data["i"])
    finally:
        writer.join()
    assert not errors
    # never an older file after a newer one
    assert seen == sorted(seen)
    assert read_json(path) == _payload(399)
    assert [p.name for p in tmp_path.iterdir()] == ["results.json"]


def test_the_reader_never_sees_partly_written_results(
    notebook: Path, task_file: Path
) -> None:
    k = KernelChannel(notebook)
    k.register([task_file], heartbeat=False)
    reader = ChannelReader(notebook, reread_interval=0)
    errors: list[BaseException] = []
    done = threading.Event()

    def write() -> None:
        try:
            for run in range(1, 300):
                results = [PASSED] * (run % 7) + [FAILED] * (run % 11)
                k.write_results(run, CellRef(str(run), run), {task_file: results})
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    writer = threading.Thread(target=write)
    try:
        k.write_results(0, None, {task_file: []})
        assert reader.poll().results is not None
        writer.start()
        runs: list[int] = []
        while not done.is_set() or len(runs) < 50:
            state = reader.poll()
            assert state.status is ChannelStatus.CONNECTED
            assert state.results is not None, "partly written results were read"
            run = state.results.run
            assert state.results.for_task(task_file) == (
                [PASSED] * (run % 7) + [FAILED] * (run % 11)
            )
            runs.append(run)
    finally:
        if writer.is_alive():
            writer.join()
        k.close()
    assert not errors
    assert runs == sorted(runs)


def test_permission_errors_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As on Windows, where a file can't be replaced while another process has it
    open, nor opened while it is being replaced."""
    path = tmp_path / "results.json"
    write_json_atomic(path, {"run": 1})
    failures = {"replace": 3, "open": 3}
    replace, real_open = os.replace, open

    def flaky_replace(src: Any, dst: Any) -> None:
        if Path(dst) == path and failures["replace"]:
            failures["replace"] -= 1
            raise PermissionError(13, "Access is denied", str(dst))
        replace(src, dst)

    def flaky_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(file) == path and failures["open"]:
            failures["open"] -= 1
            raise PermissionError(13, "Access is denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(channel, "open", flaky_open, raising=False)
    write_json_atomic(path, {"run": 2})
    assert read_json(path) == {"run": 2}
    assert failures == {"replace": 0, "open": 0}
    assert [p.name for p in tmp_path.iterdir()] == ["results.json"]


def test_a_file_that_stays_locked_is_given_up_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "results.json"
    write_json_atomic(path, {"run": 1})

    def locked(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(channel, "RETRY_FOR", 0.01)
    monkeypatch.setattr(channel, "open", locked, raising=False)
    assert read_json(path) is None
    monkeypatch.delattr(channel, "open")
    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        write_json_atomic(path, {"run": 2})
    monkeypatch.undo()
    # the temporary file is gone, and the file unchanged
    assert [p.name for p in tmp_path.iterdir()] == ["results.json"]
    assert read_json(path) == {"run": 1}
