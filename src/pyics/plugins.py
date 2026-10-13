"""
Plugins: packages installed next to pyics that extend it through pluggy hooks (see
``HookSpecs``). A plugin registers a module or object with ``hookimpl``-marked functions
as an entry point of the group "pyics", e.g. in its ``pyproject.toml``::

    [project.entry-points.pyics]
    jupyter = "pyics_jupyter.plugin"

e.g. ``pyics-jupyter``, which shows the results of tests run in a notebook's kernel
whenever a cell runs.
"""

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import pluggy
from rich.console import RenderableType

from pyics.tests.results import TestResult

if TYPE_CHECKING:
    from pyics.data.task import Task

hookspec = pluggy.HookspecMarker("pyics")
hookimpl = pluggy.HookimplMarker("pyics")


@dataclass(frozen=True)
class Overlay:
    """What a task's status shows instead of the results of its saved files (see
    ``ResultsOverlay.apply``)."""

    # the results to show, e.g. the saved ones, of which some are replaced by newer
    # ones, relabelled by their nodeid (e.g. "::adds (live)")
    results: list[TestResult]
    # lines shown below the result summary, e.g. where the results come from
    lines: list[RenderableType] = field(default_factory=list)


class ResultsOverlay(Protocol):
    """
    Results of a task's tests that the TUI shows over those of its saved files, e.g.
    from runs on code that isn't saved yet (see ``HookSpecs.pyics_results_overlay``).
    They never count as progress: whether a task is solved only depends on its saved
    files.
    """

    def poll(self) -> bool:
        """Whether there are new results since the last call, so that the task's
        status is updated (see ``Task.check_overlay``). Called at every tick of the
        TUI, so it must be cheap."""
        ...

    def apply(self, saved: list[TestResult], saved_time: float | None) -> Overlay:
        """
        What the task's status shows, given the results of its ``saved`` files, which
        were saved at ``saved_time`` (a ``time.time()``, see
        ``Task.test_results_time``).
        """
        ...


class HookSpecs:
    """The hooks pyics calls, which plugins implement with ``hookimpl``."""

    @hookspec(firstresult=True)
    def pyics_results_overlay(
        self,
        # quoted, as pluggy reads the signature, which evaluates the annotations
        task: "Task",  # noqa: UP037
    ) -> ResultsOverlay | None:
        """
        The results overlay of ``task``, or None if the plugin has none for it. Called
        once for each task the TUI shows, on the TUI's thread. The first plugin that
        returns an overlay wins.
        """


@functools.cache
def manager() -> pluggy.PluginManager:
    """
    The plugin manager, which loads the installed plugins (the entry points of the group
    "pyics") the first time it is asked for. Tests register their own plugins with it,
    and forget it with ``manager.cache_clear()``.
    """
    plugin_manager = pluggy.PluginManager("pyics")
    plugin_manager.add_hookspecs(HookSpecs)
    plugin_manager.load_setuptools_entrypoints("pyics")
    return plugin_manager
