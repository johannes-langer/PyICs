"""
The TUI's side of live results: the results overlay of a task with exercise notebooks
(see ``pyics.plugins.ResultsOverlay``), which shows the results of the notebooks'
kernels (see ``channel``) over those of the saved notebooks, and says where they come
from. They never count as progress.
"""

import dataclasses
import time
from collections.abc import Callable, Iterable

from rich.text import Text

from pyics.plugins import Overlay
from pyics.tests.results import TestResult
from pyics.util.paths import StrPath
from pyics_jupyter import channel

# the channels are read at most this often
POLL_INTERVAL = 0.5
LIVE = (
    "Live results from your notebook kernel ({run}). Save the notebook to count your "
    "progress."
)
NO_KERNEL = "No notebook kernel connected: results are from your saved notebook."
SOLVED_LIVE = "Solved in your notebook — save it to count it."


# the time, cell and results of a live run (see ``channel.KernelFile``)
type _Run = tuple[float, int | None, tuple[TestResult, ...]]


def _by_test(results: Iterable[TestResult]) -> dict[int, list[TestResult]]:
    """``results`` grouped by their test's index (its phases, in order)."""
    by_test: dict[int, list[TestResult]] = {}
    for result in results:
        if result.index is not None:
            by_test.setdefault(result.index, []).append(result)
    return by_test


def _all_pass(results: Iterable[TestResult]) -> bool:
    """Whether the tests of ``results`` pass (their calls), and there are any."""
    calls = [r.outcome == "passed" for r in results if r.when == "call"]
    return bool(calls) and all(calls)


class LiveOverlay:
    """
    The overlay of the task in ``task_file`` with the exercise ``notebooks``: each
    test's result is the newest one, i.e. that of the latest live run of the notebooks'
    kernels (of all kernels that had a heartbeat in the last ``timeout`` seconds) if it
    ran after the saved notebooks were, unless it was skipped (output and structure
    tests). ``clock`` is the current time (``time.time``), ``monotonic`` a clock for
    the ``interval`` between reads of the channels.
    """

    def __init__(
        self,
        task_file: StrPath,
        notebooks: Iterable[StrPath],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = channel.TIMEOUT,
        interval: float = POLL_INTERVAL,
    ):
        self.task = channel.same_file(task_file)
        self.directories = [channel.channel_dir(notebook) for notebook in notebooks]
        self._clock, self._monotonic = clock, monotonic
        self._timeout, self._interval = timeout, interval
        self._next_read: float | None = None
        # whether a kernel is connected, and the time, cell and results of the newest
        # live run of the task
        self._state: tuple[bool, _Run | None] = (False, None)
        for directory in self.directories:
            channel.cleanup_stale(directory, now=clock())

    def poll(self) -> bool:
        """Read the channels, unless they were read less than ``interval`` seconds ago.
        Returns whether a kernel connected or left, or there are new results."""
        if self._next_read is not None and self._monotonic() < self._next_read:
            return False
        self._next_read = self._monotonic() + self._interval
        now = self._clock()
        kernels = [
            kernel
            for directory in self.directories
            for kernel in channel.read_kernels(
                directory, now=now, timeout=self._timeout
            )
        ]
        runs: list[_Run] = [
            (k.time, k.cell, k.tasks[self.task])
            for k in kernels
            if k.time is not None and self.task in k.tasks
        ]
        state = (bool(kernels), max(runs, key=lambda run: run[0], default=None))
        changed, self._state = state != self._state, state
        return changed

    def apply(self, saved: list[TestResult], saved_time: float | None) -> Overlay:
        """The ``saved`` results, of which those that a newer live run has are replaced
        by its results, relabelled "(live)", and the lines that say where they come
        from."""
        connected, run = self._state
        lines = [] if connected else [Text(NO_KERNEL, style="dim")]
        if run is None or (saved_time is not None and saved_time >= run[0]):
            return Overlay(saved, lines)
        run_time, cell, results = run
        live, old = _by_test(results), _by_test(saved)
        shown = {
            index
            for index, phases in live.items()
            # not a test of another version of the task
            if index in old
            and phases[0].nodeid == old[index][0].nodeid
            and all(r.outcome != "skipped" for r in phases)
        }
        if not shown:
            return Overlay(saved, lines)
        merged: list[TestResult] = []
        for result in saved:
            if result.index not in shown:
                merged.append(result)
            elif result is old[result.index][0]:
                merged += [
                    dataclasses.replace(r, nodeid=f"{r.nodeid} (live)")
                    for r in live[result.index]
                ]
        at = time.strftime("%H:%M:%S", time.localtime(run_time))
        run_text = at if cell is None else f"cell {cell}, {at}"
        lines = [Text(LIVE.format(run=run_text), style="dim")]
        if _all_pass(merged) and not _all_pass(saved):
            lines.append(Text(SOLVED_LIVE, style="bright_green"))
        return Overlay(merged, lines)
