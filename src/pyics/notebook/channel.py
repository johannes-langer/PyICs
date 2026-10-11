"""
The files through which a notebook's kernel passes live test results to the TUI.

Each notebook has a channel directory next to it, ``.pyics/<notebook file name>/``
(see ``channel_dir``), in which

- each kernel running the pyics extension announces itself in ``kernel-<id>.json``
  (``KernelInfo``), removed when it shuts down cleanly,
- touches ``kernel-<id>.alive`` every ``HEARTBEAT_INTERVAL`` seconds, whose modification
  time is its heartbeat,
- and writes the results of its latest live run to ``results-<id>.json``
  (``LiveResults``),
- while the TUI writes ``control.json`` (``Control``), e.g. to turn tracing on.

Neither side ever waits for the other: the kernel's side (``KernelChannel``) writes its
files whether or not a TUI reads them, and the TUI's side (``ChannelReader``) polls the
directory. A kernel without a heartbeat for ``KERNEL_TIMEOUT`` seconds is gone, and the
files of kernels without one for ``STALE_AFTER`` seconds are removed by either side.

All files are JSON, written atomically (see ``write_json_atomic``). Files that can't be
parsed are treated as absent. Each kernel and results file names the version of this
protocol it follows as ``pyics_live``, so that a TUI ignores kernels running a pyics
it doesn't understand, and says why. The names of the files and the key ``pyics_live``
stay the same in every version, so that this can always be told.
"""

import atexit
import contextlib
import dataclasses
import enum
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Container, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any, Self

from pyics.distribution.dependencies import pyics_version
from pyics.tests.results import TestResult
from pyics.util.paths import StrPath

# the version of the files' contents, raised whenever readers of the previous version
# can't read them anymore
PROTOCOL = 1

HEARTBEAT_INTERVAL = 2.0
# a kernel without a heartbeat for this long is gone, e.g. it crashed or was killed
KERNEL_TIMEOUT = 10.0
# the files of a kernel without a heartbeat for this long are removed
STALE_AFTER = 3600.0
# files are read again at least this often, even if they seem unchanged: some file
# systems (FAT, network shares) store modification times in steps of up to 2 s
REREAD_INTERVAL = 1.0
# how long to try again when a file can't be opened or replaced, because another
# process has it open (on Windows)
RETRY_FOR = 1.0
# how often a ``ChannelReader`` removes stale files
CLEANUP_INTERVAL = 600.0

CHANNELS_DIR = ".pyics"
CONTROL_FILE = "control.json"

_KERNEL_FILE = re.compile(r"kernel-([0-9A-Za-z_-]+)\.(json|alive)")
_RESULTS_FILE = re.compile(r"results-([0-9A-Za-z_-]+)\.json")
# temporary files of ``write_json_atomic``
_TEMP_FILE = re.compile(r"\..+\.tmp")

_logger = logging.getLogger(__name__)


def channel_dir(notebook: StrPath) -> Path:
    """The channel directory of ``notebook``: ``.pyics/<notebook file name>/`` next to
    it."""
    notebook = Path(notebook)
    return notebook.parent / CHANNELS_DIR / notebook.name


def ensure_channel_dir(notebook: StrPath) -> Path:
    """
    Create the channel directory of ``notebook`` (see ``channel_dir``) if it doesn't
    exist, and a ``.gitignore`` that keeps all channel directories out of git, and
    return it.
    """
    directory = channel_dir(notebook)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # an existing one is kept, e.g. one the student changed
        with open(directory.parent / ".gitignore", "x", encoding="utf-8") as f:
            f.write("*\n")
    except OSError:
        pass  # e.g. it exists
    return directory


def _retry[T](action: Callable[[], T], *, retry_for: float | None = None) -> T:
    """
    ``action()``, tried again with a short, growing delay while it raises a
    ``PermissionError``, for at most ``retry_for`` seconds (by default ``RETRY_FOR``).
    On Windows, a file can't be replaced while another process has it open, nor opened
    while it is being replaced.
    """
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


def write_json_atomic(path: StrPath, data: Any) -> None:
    """
    Write ``data`` to ``path`` as JSON, atomically: readers see either the previous file
    or the new one, but never a partly written one. It is written to a temporary file
    in the same directory, which then replaces ``path``.

    Raises
    ------
    OSError
        If it can't be written, e.g. if the directory doesn't exist.
    """
    path = Path(path)
    # before anything is written, so that data that can't be stored changes nothing
    text = json.dumps(data, ensure_ascii=False)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        _retry(lambda: os.replace(temp, path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise


def _load(path: Path, *, retry_for: float | None = None) -> Any:
    """
    The JSON in ``path``.

    Raises
    ------
    OSError
        If it can't be read, e.g. FileNotFoundError, or PermissionError if another
        process kept it open (on Windows) for longer than ``retry_for`` seconds (see
        ``_retry``).
    ValueError
        If it isn't JSON.
    """

    def read() -> bytes:
        with open(path, "rb") as f:
            return f.read()

    return json.loads(_retry(read, retry_for=retry_for).decode("utf-8"))


def read_json(path: StrPath) -> Any | None:
    """The JSON in ``path``, or None if it doesn't exist, can't be read or isn't
    JSON."""
    try:
        return _load(Path(path))
    except OSError, ValueError:
        return None


def _remove(path: Path) -> bool:
    """Remove ``path`` if it exists. Returns whether it is gone."""
    try:
        _retry(lambda: os.remove(path), retry_for=0.2)
    except FileNotFoundError:
        pass
    except OSError:
        _logger.debug("Could not remove %s", path, exc_info=True)
        return False
    return True


def cleanup_stale(
    directory: StrPath,
    *,
    now: float | None = None,
    stale_after: float = STALE_AFTER,
    keep: Container[str] = (),
) -> list[Path]:
    """
    Remove the files that kernels left in the channel directory ``directory``, if they
    have had no heartbeat (see ``KernelChannel``) for ``stale_after`` seconds, e.g.
    after a crash, as well as old temporary files of ``write_json_atomic``.

    Parameters
    ----------
    directory: StrPath
        The channel directory (see ``channel_dir``).
    now: float | None
        The current time (see ``time.time``). By default, the time now.
    stale_after: float
        How long a kernel may go without a heartbeat until its files are removed.
    keep: Container[str]
        The ids of kernels whose files are kept, e.g. the caller's.

    Returns
    -------
    list[Path]
        The files that were removed.
    """
    directory = Path(directory)
    now = time.time() if now is None else now
    try:
        names = [entry.name for entry in os.scandir(directory)]
    except OSError:
        return []

    def last_modified(path: Path) -> float | None:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    files: dict[str, list[Path]] = {}
    stale: list[Path] = []
    for name in names:
        if match := _KERNEL_FILE.fullmatch(name) or _RESULTS_FILE.fullmatch(name):
            files.setdefault(match[1], []).append(directory / name)
        elif _TEMP_FILE.fullmatch(name):
            modified = last_modified(directory / name)
            if modified is not None and now - modified > stale_after:
                stale.append(directory / name)

    for kernel_id, paths in files.items():
        if kernel_id in keep:
            continue
        # the heartbeat, or without one, the latest sign of life
        alive = directory / f"kernel-{kernel_id}.alive"
        times = [last_modified(alive)] if alive in paths else map(last_modified, paths)
        # files that just disappeared, or can't be looked at right now, don't count
        known = [t for t in times if t is not None]
        if known and all(now - t > stale_after for t in known):
            # the kernel file first, so that the kernel is gone at once for readers
            stale += sorted(paths, key=lambda p: (p.suffix != ".json", p.name))

    return [path for path in stale if _remove(path)]


def _task_key(task_file: StrPath) -> str:
    """How results name ``task_file``: as an absolute path with forward slashes."""
    return Path(os.path.abspath(task_file)).as_posix()


def _same_file(path: StrPath) -> str:
    """``path`` in a form that is the same for all paths to the same file (on the same
    machine), to compare paths written by different processes."""
    return os.path.normcase(os.path.realpath(path))


def _get(data: Mapping[str, Any], name: str, *types: type, default: Any = None) -> Any:
    """``data[name]`` if it is of one of ``types`` (JSON's booleans only count as
    ``bool``), otherwise ``default``."""
    value = data.get(name, default)
    if isinstance(value, types) and (bool in types or not isinstance(value, bool)):
        return value
    return default


def _require(data: Mapping[str, Any], name: str, *types: type) -> Any:
    """``data[name]``, which must be of one of ``types``."""
    value = _get(data, name, *types, default=...)
    if value is ...:
        raise ValueError(f"{name!r} is missing or malformed")
    return value


def _protocol_of(data: Any) -> int | None:
    """The protocol version (``pyics_live``) a file's contents follow, or None if it
    isn't a channel file at all."""
    if not isinstance(data, Mapping):
        return None
    return _get(data, "pyics_live", int)


def _incompatible(what: str, version: str | None, protocol: object) -> str:
    """Why ``what``, written by pyics ``version`` following another ``protocol``, is
    ignored, and how to fix that."""
    return (
        f"Ignoring {what}: it runs pyics {version or '(unknown version)'} (live "
        f"protocol {protocol}), which this pyics ({pyics_version()}, protocol "
        f"{PROTOCOL}) can't understand. Run `pyics install_kernel`, choose the "
        f'"Python (pyics)" kernel for the notebook and restart it, so that both use '
        f"the same pyics."
    )


@dataclasses.dataclass(frozen=True)
class CellRef:
    """
    The notebook cell whose run led to live results.

    Attributes
    ----------
    id: str | None
        The cell's id (nbformat 4.5), if known.
    number: int | None
        Its position in the saved notebook, counting all cells from 1, if it is there.
    """

    id: str | None = None
    number: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "number": self.number}

    @classmethod
    def from_json(cls, data: Any) -> Self | None:
        if not isinstance(data, Mapping):
            return None
        return cls(id=_get(data, "id", str), number=_get(data, "number", int))


@dataclasses.dataclass(frozen=True)
class Control:
    """
    What the TUI asks of the kernels of a notebook (``control.json``).

    Attributes
    ----------
    tracing: bool
        Trace the tests of live runs, to show the traces of failed tests.
    paused: bool
        Don't run tests when cells run.
    """

    tracing: bool = False
    paused: bool = False

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> Self:
        """The control stored as ``data``, with defaults for what is missing."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            tracing=_get(data, "tracing", bool, default=False),
            paused=_get(data, "paused", bool, default=False),
        )


@dataclasses.dataclass(frozen=True)
class KernelInfo:
    """
    A kernel running the pyics extension for a notebook (``kernel-<id>.json``).

    Attributes
    ----------
    kernel_id: str
        A new id for each time the extension is loaded, i.e. for each kernel start.
    protocol: int
        The protocol version the kernel follows (``pyics_live``, see ``compatible``).
    pyics_version: str | None
        The version of pyics the kernel runs.
    pid: int | None
        The kernel's process id.
    executable: str | None
        The kernel's Python interpreter.
    started: float
        When the kernel registered (see ``time.time``).
    task_files: tuple[str, ...]
        The task files whose tests the kernel runs, as absolute paths with forward
        slashes.
    connection_file: str | None
        The kernel's Jupyter connection file, which frontends keep when they restart
        the kernel: a newer kernel with the same one has replaced this one.
    """

    kernel_id: str
    protocol: int
    pyics_version: str | None = None
    pid: int | None = None
    executable: str | None = None
    started: float = 0.0
    task_files: tuple[str, ...] = ()
    connection_file: str | None = None

    @property
    def compatible(self) -> bool:
        """Whether the kernel follows this pyics's protocol (see ``PROTOCOL``)."""
        return self.protocol == PROTOCOL

    def to_json(self) -> dict[str, Any]:
        return {
            "pyics_live": self.protocol,
            "pyics_version": self.pyics_version,
            "kernel_id": self.kernel_id,
            "pid": self.pid,
            "executable": self.executable,
            "started": self.started,
            "task_files": list(self.task_files),
            "connection_file": self.connection_file,
        }

    @classmethod
    def from_json(cls, data: Any, kernel_id: str) -> Self:
        """
        The kernel stored as ``data`` in the file of the kernel ``kernel_id``. Of a
        kernel following another protocol, only what can be told is kept.

        Raises
        ------
        ValueError
            If ``data`` isn't a kernel file.
        """
        if (protocol := _protocol_of(data)) is None:
            raise ValueError("not a kernel file")
        if protocol != PROTOCOL:
            return cls(
                kernel_id=kernel_id,
                protocol=protocol,
                pyics_version=_get(data, "pyics_version", str),
                pid=_get(data, "pid", int),
                executable=_get(data, "executable", str),
            )
        if _require(data, "kernel_id", str) != kernel_id:
            raise ValueError("the kernel file of another kernel")
        task_files = _get(data, "task_files", list, default=[])
        return cls(
            kernel_id=kernel_id,
            protocol=protocol,
            pyics_version=_get(data, "pyics_version", str),
            pid=_get(data, "pid", int),
            executable=_get(data, "executable", str),
            started=float(_require(data, "started", int, float)),
            task_files=tuple(f for f in task_files if isinstance(f, str)),
            connection_file=_get(data, "connection_file", str),
        )

    def describe(self) -> str:
        """The kernel as the TUI names it, e.g. "the notebook kernel (pid 1234,
        /usr/bin/python3)"."""
        details = [f"pid {self.pid}"] if self.pid is not None else []
        details += [self.executable] if self.executable else []
        return "the notebook kernel" + (f" ({', '.join(details)})" if details else "")


@dataclasses.dataclass(frozen=True)
class LiveResults:
    """
    The results of a kernel's latest live run (``results-<id>.json``).

    Attributes
    ----------
    kernel_id: str
        The kernel that ran the tests.
    run: int
        Counts the kernel's live runs, to tell new results from old ones.
    time: float
        When the tests were run (see ``time.time``).
    cell: CellRef | None
        The cell whose run led to the results.
    tasks: Mapping[str, tuple[TestResult, ...]]
        The results of each task's tests, by the task file's absolute path with forward
        slashes (see ``for_task``).
    """

    kernel_id: str
    run: int
    time: float
    cell: CellRef | None
    tasks: Mapping[str, tuple[TestResult, ...]]

    def for_task(self, task_file: StrPath) -> list[TestResult] | None:
        """The results of the tests of the task in ``task_file``, or None if they
        weren't run."""
        wanted = _same_file(task_file)
        for key, results in self.tasks.items():
            if _same_file(key) == wanted:
                return list(results)
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "pyics_live": PROTOCOL,
            "kernel_id": self.kernel_id,
            "run": self.run,
            "time": self.time,
            "cell": None if self.cell is None else self.cell.to_json(),
            "tasks": {
                task: [result.to_json() for result in results]
                for task, results in self.tasks.items()
            },
        }

    @classmethod
    def from_json(cls, data: Any, kernel_id: str) -> Self:
        """
        The results stored as ``data`` in the results file of the kernel
        ``kernel_id``, which follow this pyics's protocol.

        Raises
        ------
        ValueError
            If ``data`` isn't a results file of that kernel, of this protocol.
        """
        if _protocol_of(data) != PROTOCOL:
            raise ValueError(f"not a results file of protocol {PROTOCOL}")
        if _require(data, "kernel_id", str) != kernel_id:
            raise ValueError("the results file of another kernel")
        tasks: dict[str, tuple[TestResult, ...]] = {}
        for task, results in _require(data, "tasks", Mapping).items():
            if not isinstance(task, str) or not isinstance(results, list):
                raise ValueError("'tasks' is malformed")
            tasks[task] = tuple(TestResult.from_json(result) for result in results)
        return cls(
            kernel_id=kernel_id,
            run=_require(data, "run", int),
            time=float(_require(data, "time", int, float)),
            cell=CellRef.from_json(data.get("cell")),
            tasks=tasks,
        )


class ChannelStatus(enum.StrEnum):
    """Whether live results are shown, and if not, why not (see ``ChannelState``)."""

    # a kernel is connected (its results may still be missing)
    CONNECTED = "connected"
    # no kernel runs the pyics extension for the notebook
    NO_KERNEL = "no kernel"
    # the kernel whose results were shown has stopped sending heartbeats
    TIMED_OUT = "timed out"
    # a new kernel replaced the one whose results were shown, and has no results yet
    RESTARTED = "restarted"
    # only kernels running a pyics with another protocol are there
    INCOMPATIBLE = "incompatible"


_REASONS = {
    ChannelStatus.NO_KERNEL: (
        "No notebook kernel connected: results are from your saved notebook."
    ),
    ChannelStatus.TIMED_OUT: (
        "Your notebook kernel stopped responding: results are from your saved notebook."
    ),
    ChannelStatus.RESTARTED: (
        "Your notebook kernel restarted: run your cells to see live results."
    ),
}


@dataclasses.dataclass(frozen=True)
class ChannelState:
    """
    What the channel directory of a notebook says, as seen by ``ChannelReader.poll``.

    Attributes
    ----------
    status: ChannelStatus
        Whether a kernel is connected, and if not, why not.
    kernel: KernelInfo | None
        The connected kernel: of the kernels with a heartbeat, the one whose results
        are newest, or without results, the one started last. Also set when the
        ``status`` is RESTARTED (the new kernel).
    results: LiveResults | None
        The connected kernel's latest results, if it has any.
    reason: str | None
        Why there are no live results, and for INCOMPATIBLE, how to fix it. None while
        a kernel is CONNECTED.
    kernels: tuple[KernelInfo, ...]
        All kernels with a heartbeat that follow this pyics's protocol, oldest first.
    ignored: tuple[str, ...]
        Why files are ignored, e.g. kernels of other protocols.
    """

    status: ChannelStatus
    kernel: KernelInfo | None = None
    results: LiveResults | None = None
    reason: str | None = None
    kernels: tuple[KernelInfo, ...] = ()
    ignored: tuple[str, ...] = ()


class KernelChannel:
    """
    A kernel's side of the channel of a notebook (see the module's documentation): what
    the pyics extension in a kernel uses to pass its live results to the TUI.

    ``register`` announces the kernel and starts its heartbeat, ``write_results``
    writes the results of each live run, ``read_control`` tells what the TUI asks for,
    and ``close`` (called at exit, too) removes the kernel's files.
    """

    def __init__(
        self,
        notebook: StrPath,
        *,
        kernel_id: str | None = None,
        connection_file: str | None = None,
        clock: Callable[[], float] = time.time,
        interval: float = HEARTBEAT_INTERVAL,
        stale_after: float = STALE_AFTER,
    ):
        """
        Parameters
        ----------
        notebook: StrPath
            The notebook the kernel runs.
        kernel_id: str | None (optional, keyword only)
            The kernel's id. By default, a new random one.
        connection_file: str | None (optional, keyword only)
            The kernel's Jupyter connection file (see ``current_connection_file``),
            which tells a TUI that a new kernel replaced this one when the frontend
            restarts it.
        clock: Callable[[], float] (optional, keyword only)
            The current time, ``time.time`` by default.
        interval: float (optional, keyword only)
            The seconds between heartbeats.
        stale_after: float (optional, keyword only)
            Remove other kernels' files when they have had no heartbeat for this long
            (see ``cleanup_stale``).
        """
        self.notebook = Path(os.path.abspath(notebook))
        self.directory = channel_dir(self.notebook)
        self.kernel_id = kernel_id or uuid.uuid4().hex
        self.connection_file = connection_file
        self.kernel_file = self.directory / f"kernel-{self.kernel_id}.json"
        self.alive_file = self.directory / f"kernel-{self.kernel_id}.alive"
        self.results_file = self.directory / f"results-{self.kernel_id}.json"
        self.task_files: tuple[str, ...] = ()
        # when the kernel registered, None before
        self.started: float | None = None

        self._clock = clock
        self._interval = interval
        self._stale_after = stale_after
        # makes sure no heartbeat recreates the files after ``close``
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def info(self) -> KernelInfo:
        """What the kernel file says about this kernel."""
        return KernelInfo(
            kernel_id=self.kernel_id,
            protocol=PROTOCOL,
            pyics_version=pyics_version(),
            pid=os.getpid(),
            executable=sys.executable,
            started=self.started or 0.0,
            task_files=self.task_files,
            connection_file=self.connection_file,
        )

    def register(
        self, task_files: Iterable[StrPath] = (), *, heartbeat: bool = True
    ) -> None:
        """
        Announce the kernel, with the task files whose tests it runs: create the
        channel directory, remove the stale files of other kernels, write the kernel
        file and the first heartbeat, and start the heartbeat thread (unless
        ``heartbeat`` is False). Call it again when the tasks change, e.g. after a task
        file was named explicitly.

        Raises
        ------
        OSError
            If the files can't be written, e.g. in a read-only directory.
        """
        with self._lock:
            self.task_files = tuple(_task_key(f) for f in task_files)
            ensure_channel_dir(self.notebook)
            if self.started is None:
                self.started = self._clock()
                self.cleanup_stale()
            self._closed = False
            # the heartbeat first, so that a reader never finds the kernel without one
            self._beat()
            self._write(self.kernel_file, self.info.to_json())
        if heartbeat:
            self.start_heartbeat()
        # registering it again doesn't call it twice
        atexit.unregister(self.close)
        atexit.register(self.close)

    def _write(self, path: Path, data: Any) -> None:
        """Write ``data`` to ``path``, recreating the channel directory if it was
        removed meanwhile."""
        try:
            write_json_atomic(path, data)
        except FileNotFoundError:
            ensure_channel_dir(self.notebook)
            write_json_atomic(path, data)

    def _beat(self) -> None:
        """Set the heartbeat file's modification time to now, creating it if needed."""
        now = self._clock()
        try:
            os.utime(self.alive_file, (now, now))
        except FileNotFoundError:
            ensure_channel_dir(self.notebook)
            self.alive_file.touch()
            os.utime(self.alive_file, (now, now))

    def beat(self) -> None:
        """
        Send a heartbeat (the heartbeat thread does so every ``interval`` seconds). If
        the kernel file was removed meanwhile, e.g. as stale after the computer slept
        for a long time, it is written again. Does nothing before ``register`` and after
        ``close``.
        """
        with self._lock:
            if self._closed or self.started is None:
                return
            self._beat()
            if not self.kernel_file.exists():
                self._write(self.kernel_file, self.info.to_json())

    def start_heartbeat(self) -> None:
        """Start the daemon thread that sends a heartbeat every ``interval`` seconds,
        if it isn't running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._send_heartbeats, name="pyics-heartbeat", daemon=True
        )
        self._thread.start()

    def _send_heartbeats(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.beat()
            except OSError:
                # tried again at the next heartbeat
                _logger.debug("Heartbeat failed", exc_info=True)

    def write_results(
        self,
        run: int,
        cell: CellRef | None,
        results_by_task: Mapping[StrPath, Iterable[TestResult]],
    ) -> None:
        """
        Write the results of a live run, replacing those of the previous one.

        Parameters
        ----------
        run: int
            Counts the live runs of the kernel, so that readers can tell new results
            from old ones.
        cell: CellRef | None
            The cell whose run led to the live run.
        results_by_task: Mapping[StrPath, Iterable[TestResult]]
            The results of the tests of each task, by the path of its task file.

        Raises
        ------
        OSError
            If the results can't be written.
        """
        results = LiveResults(
            kernel_id=self.kernel_id,
            run=run,
            time=self._clock(),
            cell=cell,
            tasks={_task_key(t): tuple(r) for t, r in results_by_task.items()},
        )
        self._write(self.results_file, results.to_json())

    def read_control(self) -> Control:
        """What the TUI asks for (see ``Control``), the defaults if it asks nothing."""
        return Control.from_json(read_json(self.directory / CONTROL_FILE))

    def cleanup_stale(self) -> list[Path]:
        """Remove the stale files of other kernels (see ``cleanup_stale``)."""
        return cleanup_stale(
            self.directory,
            now=self._clock(),
            stale_after=self._stale_after,
            keep={self.kernel_id},
        )

    def close(self) -> None:
        """Stop the heartbeat and remove the kernel's files, so that readers know at
        once that it is gone. Called at exit, too."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            self._closed = True
            for path in (self.kernel_file, self.results_file, self.alive_file):
                _remove(path)
        atexit.unregister(self.close)


def current_connection_file() -> str | None:
    """The connection file of the Jupyter kernel this code runs in, None outside of
    one."""
    try:
        from ipykernel.connect import get_connection_file

        return os.path.abspath(get_connection_file())
    except Exception:  # no kernel, or ipykernel changed
        return None


def _stat_key(path: Path) -> tuple[int, int] | None:
    """
    ``path``'s modification time and size, which change when it is replaced, or None if
    they can't be told right now (e.g. while it is being replaced, on Windows).

    Raises
    ------
    FileNotFoundError
        If there is no file at ``path``.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


@dataclasses.dataclass
class _Cached:
    """A file's contents as last read, see ``ChannelReader._read``."""

    key: tuple[int, int]
    read_at: float
    value: Any


@dataclasses.dataclass(frozen=True)
class _Ignored:
    """A file that is ignored, and why."""

    reason: str


def _parse_results(data: Any, kernel_id: str) -> LiveResults | _Ignored:
    """The results of the kernel ``kernel_id`` stored as ``data``, or why they are
    ignored, if they follow another protocol."""
    protocol = _protocol_of(data)
    if protocol is not None and protocol != PROTOCOL:
        what = f"the live results of kernel {kernel_id}"
        return _Ignored(_incompatible(what, _get(data, "pyics_version", str), protocol))
    return LiveResults.from_json(data, kernel_id)


class ChannelReader:
    """
    The TUI's side of the channel of a notebook (see the module's documentation): which
    kernel is connected, and its latest results (see ``poll``), and what the TUI asks
    of the kernels (see ``write_control``).
    """

    def __init__(
        self,
        notebook: StrPath,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = KERNEL_TIMEOUT,
        reread_interval: float = REREAD_INTERVAL,
        stale_after: float = STALE_AFTER,
        cleanup_interval: float = CLEANUP_INTERVAL,
    ):
        """
        Parameters
        ----------
        notebook: StrPath
            The notebook.
        clock: Callable[[], float] (optional, keyword only)
            The current time, ``time.time`` by default, compared with heartbeats.
        monotonic: Callable[[], float] (optional, keyword only)
            A clock for intervals, ``time.monotonic`` by default.
        timeout: float (optional, keyword only)
            A kernel without a heartbeat for this long is gone.
        reread_interval: float (optional, keyword only)
            Read files again after this long, even if they seem unchanged.
        stale_after: float (optional, keyword only)
            Remove the files of kernels without a heartbeat for this long (see
            ``cleanup_stale``).
        cleanup_interval: float (optional, keyword only)
            How often ``poll`` removes stale files.
        """
        self.notebook = Path(os.path.abspath(notebook))
        self.directory = channel_dir(self.notebook)
        self._clock = clock
        self._monotonic = monotonic
        self._timeout = timeout
        self._reread_interval = reread_interval
        self._stale_after = stale_after
        self._cleanup_interval = cleanup_interval
        self._next_cleanup: float | None = None

        self._listing: _Cached | None = None
        self._files: dict[str, _Cached] = {}
        self._heartbeats: dict[str, float] = {}
        # the kernel whose results were last returned by ``poll``
        self._shown: KernelInfo | None = None
        self._state: ChannelState | None = None

    def _cached(self, cached: _Cached | None, key: tuple[int, int] | None) -> bool:
        """Whether ``cached`` is still current for a file whose stat key is ``key``."""
        if cached is None:
            return False
        if key is None:  # can't tell, so keep it until it can be read
            return True
        fresh = self._monotonic() - cached.read_at < self._reread_interval
        return cached.key == key and fresh

    def _names(self) -> list[str]:
        """The names of the files in the channel directory."""
        try:
            key = _stat_key(self.directory)
        except FileNotFoundError:
            self._listing = None
            return []
        if not self._cached(self._listing, key):
            try:
                names = sorted(entry.name for entry in os.scandir(self.directory))
            except OSError:
                return [] if self._listing is None else self._listing.value
            self._listing = _Cached(key or (0, 0), self._monotonic(), names)
        assert self._listing is not None
        return self._listing.value

    def _read[T](self, name: str, parse: Callable[[Any], T]) -> T | None:
        """
        The contents of the file ``name`` in the channel directory, parsed with
        ``parse``, or None if it doesn't exist or can't be parsed (``parse`` raises a
        ValueError). It is only read again when it changed (see ``_stat_key``), or after
        ``reread_interval`` seconds.
        """
        path = self.directory / name
        cached = self._files.get(name)
        try:
            key = _stat_key(path)
        except FileNotFoundError:
            self._files.pop(name, None)
            return None
        if self._cached(cached, key):
            assert cached is not None
            return cached.value
        try:
            # briefly, so that the TUI stays responsive: it tries again at the next poll
            value = parse(_load(path, retry_for=0.05))
        except FileNotFoundError:
            self._files.pop(name, None)
            return None
        except OSError:
            return None if cached is None else cached.value
        except ValueError, TypeError:
            value = None  # treated as absent until it changes
        self._files[name] = _Cached(key or (0, 0), self._monotonic(), value)
        return value

    def _heartbeat(self, kernel_id: str) -> float | None:
        """The time of the latest heartbeat of the kernel ``kernel_id``, if it has
        one."""
        try:
            beat = os.stat(self.directory / f"kernel-{kernel_id}.alive").st_mtime
        except FileNotFoundError:
            self._heartbeats.pop(kernel_id, None)
            return None
        except OSError:  # e.g. while it is being touched, on Windows
            return self._heartbeats.get(kernel_id)
        self._heartbeats[kernel_id] = beat
        return beat

    def poll(self) -> ChannelState:
        """
        The current state of the channel: the connected kernel and its latest
        results, or why there is none.

        Cheap enough to call at every tick: files are only read when they changed, or
        after ``reread_interval`` seconds. As long as nothing changes, it returns the
        same ``ChannelState`` object.
        """
        if self._next_cleanup is None or self._monotonic() >= self._next_cleanup:
            self._next_cleanup = self._monotonic() + self._cleanup_interval
            self.cleanup_stale()

        now = self._clock()
        names = self._names()
        live: list[KernelInfo] = []
        timed_out: set[str] = set()
        incompatible: list[str] = []
        ignored: list[str] = []
        for name in names:
            if not (match := _KERNEL_FILE.fullmatch(name)) or match[2] != "json":
                continue
            kernel_id = match[1]
            info = self._read(name, partial(KernelInfo.from_json, kernel_id=kernel_id))
            if info is None:
                continue
            beat = self._heartbeat(kernel_id)
            if beat is None or now - beat > self._timeout:
                if info.compatible:
                    timed_out.add(kernel_id)
                continue
            if not info.compatible:
                reason = _incompatible(
                    info.describe(), info.pyics_version, info.protocol
                )
                incompatible.append(reason)
                continue
            live.append(info)

        # a frontend restarting a kernel keeps its connection file
        live = [
            info
            for info in live
            if not any(
                other.started > info.started
                and info.connection_file is not None
                and other.connection_file == info.connection_file
                for other in live
            )
        ]
        live.sort(key=lambda info: info.started)

        results: dict[str, LiveResults] = {}
        for info in live:
            name = f"results-{info.kernel_id}.json"
            found = self._read(name, partial(_parse_results, kernel_id=info.kernel_id))
            if isinstance(found, _Ignored):
                ignored.append(found.reason)
            elif found is not None:
                results[info.kernel_id] = found

        connected: KernelInfo | None = None
        if with_results := [info for info in live if info.kernel_id in results]:
            connected = max(
                with_results,
                key=lambda info: (results[info.kernel_id].time, info.started),
            )
        elif live:
            connected = live[-1]

        latest = None if connected is None else results.get(connected.kernel_id)
        shown = self._shown
        if connected is not None and latest is not None:
            status = ChannelStatus.CONNECTED
            self._shown = connected
        elif connected is not None:
            restarted = (
                shown is not None
                and shown.kernel_id != connected.kernel_id
                and connected.started > shown.started
            )
            status = ChannelStatus.RESTARTED if restarted else ChannelStatus.CONNECTED
        elif incompatible:
            status = ChannelStatus.INCOMPATIBLE
        elif shown is not None and shown.kernel_id in timed_out:
            status = ChannelStatus.TIMED_OUT
        else:
            status = ChannelStatus.NO_KERNEL

        reason = _REASONS.get(status)
        if status is ChannelStatus.INCOMPATIBLE:
            reason = incompatible[0]
        state = ChannelState(
            status=status,
            kernel=connected,
            results=latest,
            reason=reason,
            kernels=tuple(live),
            ignored=tuple(incompatible + ignored),
        )
        if state != self._state:
            self._state = state
        return self._state

    def read_control(self) -> Control:
        """What the TUI asks of the kernels (see ``Control``)."""
        return Control.from_json(read_json(self.directory / CONTROL_FILE))

    def write_control(
        self, *, tracing: bool | None = None, paused: bool | None = None
    ) -> Control:
        """
        Ask the kernels to trace their live runs, or to pause them (see ``Control``).
        What isn't given stays as it is. Returns what is asked now.

        Raises
        ------
        OSError
            If it can't be written.
        """
        current = self.read_control()
        control = Control(
            tracing=current.tracing if tracing is None else tracing,
            paused=current.paused if paused is None else paused,
        )
        ensure_channel_dir(self.notebook)
        write_json_atomic(self.directory / CONTROL_FILE, control.to_json())
        return control

    def cleanup_stale(self) -> list[Path]:
        """Remove the stale files of kernels (see ``cleanup_stale``)."""
        return cleanup_stale(
            self.directory, now=self._clock(), stale_after=self._stale_after
        )
