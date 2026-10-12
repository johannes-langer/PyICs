"""
The files through which a notebook's kernel passes its live results to the TUI.

Each kernel that runs the tests of a notebook's tasks (see ``extension``) keeps one file
in the notebook's channel directory, ``.pyics/<notebook file name>/`` next to it (see
``channel_dir``): ``kernel-<id>.json``, holding the results of its latest live run
(``KernelFile``). The kernel replaces it atomically after each run (see ``write_json``)
and removes it when it shuts down. Its modification time is the kernel's heartbeat,
renewed every ``HEARTBEAT_INTERVAL`` seconds: a kernel without one for ``TIMEOUT``
seconds is gone (e.g. killed), and its file is removed after ``STALE_AFTER`` seconds.

Neither side waits for the other: the kernel writes whether or not a TUI reads, and the
TUI polls the directory (see ``read_kernels``). A file that can't be read, or whose
``version`` isn't ``VERSION`` (e.g. written by another version of pyics-jupyter), is
ignored.
"""

import atexit
import contextlib
import dataclasses
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Self

from pyics.tests.results import TestResult
from pyics.util.paths import StrPath

# the version of the files' contents, raised whenever readers can't read them anymore
VERSION = 1
HEARTBEAT_INTERVAL = 2.0
# a kernel without a heartbeat for this long is gone
TIMEOUT = 10.0
# the files of kernels without a heartbeat for this long are removed
STALE_AFTER = 3600.0
# how long to try again when a file can't be opened or replaced, because another
# process has it open (on Windows)
RETRY_FOR = 1.0
CHANNELS_DIR = ".pyics"


def channel_dir(notebook: StrPath) -> Path:
    """The channel directory of ``notebook``: ``.pyics/<notebook file name>/`` next to
    it."""
    notebook = Path(os.path.abspath(notebook))
    return notebook.parent / CHANNELS_DIR / notebook.name


def same_file(path: StrPath) -> str:
    """``path`` in a form that is the same for all paths to the same file, e.g. to
    name a task file in results written by another process."""
    return os.path.normcase(os.path.realpath(path))


def _retry[T](action: Callable[[], T], retry_for: float | None = None) -> T:
    """``action()``, tried again with a short, growing delay while it raises a
    ``PermissionError``, for at most ``retry_for`` seconds (``RETRY_FOR`` by default):
    on Windows, a file can't be replaced while another process has it open, nor opened
    while it is replaced."""
    deadline = time.monotonic() + (RETRY_FOR if retry_for is None else retry_for)
    delay = 0.001
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(2 * delay, 0.05)


def write_json(path: Path, data: Any) -> None:
    """
    Write ``data`` to ``path`` as JSON, atomically: readers see the previous file or
    the new one, never a partly written one.

    Raises
    ------
    OSError
        If it can't be written, e.g. if the directory doesn't exist.
    """
    text = json.dumps(data, ensure_ascii=False)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        _retry(lambda: os.replace(temp, path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise


def read_json(path: Path, retry_for: float | None = None) -> Any | None:
    """The JSON in ``path``, or None if it doesn't exist, can't be read (see
    ``_retry``) or isn't JSON."""
    try:
        return json.loads(_retry(path.read_bytes, retry_for))
    except OSError, ValueError:
        return None


def cleanup_stale(
    directory: Path, *, now: float, stale_after: float = STALE_AFTER
) -> None:
    """Remove the files in the channel directory ``directory`` that haven't changed for
    ``stale_after`` seconds: of kernels that crashed, and temporary files of
    ``write_json``."""
    with contextlib.suppress(OSError), os.scandir(directory) as entries:
        for entry in list(entries):
            with contextlib.suppress(OSError):
                if now - entry.stat().st_mtime > stale_after:
                    os.remove(entry.path)


@dataclasses.dataclass(frozen=True)
class KernelFile:
    """
    What the file of a kernel says (``kernel-<id>.json``).

    Attributes
    ----------
    kernel_id: str
        A new id for each kernel start.
    time: float | None
        When its latest live run ran (see ``time.time``), None before its first.
    cell: int | None
        The position (from 1) in the saved notebook of the cell whose run led to it, if
        it is there.
    tasks: Mapping[str, tuple[TestResult, ...]]
        The results of the tests of each task, by its task file (see ``same_file``).
    """

    kernel_id: str
    time: float | None = None
    cell: int | None = None
    tasks: Mapping[str, tuple[TestResult, ...]] = dataclasses.field(
        default_factory=dict
    )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "kernel_id": self.kernel_id,
            "time": self.time,
            "cell": self.cell,
            "tasks": {key: [r.to_json() for r in rs] for key, rs in self.tasks.items()},
        }

    @classmethod
    def from_json(cls, data: Any) -> Self:
        """
        The file stored as ``data`` by ``to_json``.

        Raises
        ------
        ValueError
            If ``data`` isn't such a file, of this ``VERSION``.
        """
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise ValueError(f"not a kernel file of version {VERSION}")
        kernel_id, cell, tasks = (
            data.get("kernel_id"),
            data.get("cell"),
            data.get("tasks"),
        )
        run_time = data.get("time")
        if not (
            isinstance(kernel_id, str)
            and isinstance(run_time, int | float | None)
            and isinstance(cell, int | None)
            and isinstance(tasks, dict)
            and all(isinstance(results, list) for results in tasks.values())
        ):
            raise ValueError("a malformed kernel file")
        return cls(
            kernel_id,
            None if run_time is None else float(run_time),
            cell,
            {key: tuple(map(TestResult.from_json, rs)) for key, rs in tasks.items()},
        )


def read_kernels(
    directory: Path, *, now: float, timeout: float = TIMEOUT
) -> list[KernelFile]:
    """The files of the kernels in the channel directory ``directory`` that had a
    heartbeat in the last ``timeout`` seconds before ``now``. Files that can't be read
    are left out."""
    kernels = []
    with contextlib.suppress(OSError), os.scandir(directory) as entries:
        for entry in list(entries):
            if not (entry.name.startswith("kernel-") and entry.name.endswith(".json")):
                continue
            try:
                if now - entry.stat().st_mtime > timeout:
                    continue
                # briefly, so that the TUI stays responsive: it reads again soon
                kernels.append(KernelFile.from_json(read_json(Path(entry.path), 0.05)))
            except OSError, ValueError, TypeError:
                continue
    return kernels


class KernelChannel:
    """
    A kernel's side of the channel of ``notebook``: ``open`` writes its file and starts
    its heartbeat, ``write`` writes the results of each live run, and ``close`` (called
    at exit, too) removes the file.

    ``clock`` is the current time (``time.time``), and ``interval`` the seconds between
    heartbeats.
    """

    def __init__(
        self,
        notebook: StrPath,
        *,
        clock: Callable[[], float] = time.time,
        interval: float = HEARTBEAT_INTERVAL,
    ):
        self.directory = channel_dir(notebook)
        self.latest = KernelFile(uuid.uuid4().hex)
        self.path = self.directory / f"kernel-{self.latest.kernel_id}.json"
        self._clock = clock
        self._interval = interval
        # held while the file is written, so that a heartbeat never recreates it after
        # ``close``
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._closed.set()
        self._thread: threading.Thread | None = None

    def open(self, *, heartbeat: bool = True) -> None:
        """
        Create the channel directory (with a ``.gitignore`` in ``.pyics/`` that keeps
        it out of git), remove stale files, write the kernel's file, and start the
        heartbeat thread (unless ``heartbeat`` is False).

        Raises
        ------
        OSError
            If the file can't be written, e.g. in a read-only directory.
        """
        with self._lock:
            self._closed.clear()
            self.directory.mkdir(parents=True, exist_ok=True)
            gitignore = self.directory.parent / ".gitignore"
            with contextlib.suppress(OSError), open(gitignore, "x") as f:
                f.write("*\n")  # unless it exists
            cleanup_stale(self.directory, now=self._clock())
            self._write()
        if heartbeat and self._thread is None:
            self._thread = threading.Thread(
                target=self._beat_until_closed, name="pyics-heartbeat", daemon=True
            )
            self._thread.start()
        atexit.register(self.close)

    def _write(self) -> None:
        """Write the kernel's file, recreating the directory if it was removed, and
        send a heartbeat. The lock must be held."""
        self.directory.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self.latest.to_json())
        now = self._clock()
        _retry(lambda: os.utime(self.path, (now, now)))

    def write(
        self, results: Mapping[StrPath, Iterable[TestResult]], cell: int | None
    ) -> None:
        """
        Write the ``results`` of a live run (of each task's tests, by task file), which
        the cell at the position ``cell`` in the saved notebook led to, if known.

        Raises
        ------
        OSError
            If they can't be written.
        """
        with self._lock:
            tasks = {same_file(path): tuple(rs) for path, rs in results.items()}
            self.latest = KernelFile(self.latest.kernel_id, self._clock(), cell, tasks)
            if not self._closed.is_set():
                self._write()

    def beat(self) -> None:
        """Send a heartbeat (the heartbeat thread does so every ``interval`` seconds),
        writing the file again if it was removed, e.g. as stale after the computer
        slept. Does nothing while closed."""
        with self._lock:
            if self._closed.is_set():
                return
            now = self._clock()
            try:
                _retry(lambda: os.utime(self.path, (now, now)))
            except FileNotFoundError:
                self._write()

    def _beat_until_closed(self) -> None:
        while not self._closed.wait(self._interval):
            with contextlib.suppress(OSError):  # tried again at the next heartbeat
                self.beat()

    def close(self) -> None:
        """Stop the heartbeat and remove the kernel's file, so that the TUI knows at
        once that the kernel is gone."""
        with self._lock:
            self._closed.set()
            with contextlib.suppress(OSError):
                _retry(lambda: os.remove(self.path), 0.2)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        atexit.unregister(self.close)
