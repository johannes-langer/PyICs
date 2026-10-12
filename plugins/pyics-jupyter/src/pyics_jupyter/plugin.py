"""
The plugin pyics loads by its entry point (group "pyics", see ``pyics.plugins``): the
results overlay of tasks with exercise notebooks, which shows the live results of the
notebooks' kernels in the TUI (see ``overlay``).
"""

from pyics.data.task import Task
from pyics.notebook import cells
from pyics.plugins import ResultsOverlay, hookimpl
from pyics_jupyter.overlay import LiveOverlay


@hookimpl
def pyics_results_overlay(task: Task) -> ResultsOverlay | None:
    """The live results overlay of ``task``, None if it has no exercise notebooks."""
    notebooks = [f for f in task.exercise_files if cells.is_notebook(f)]
    return LiveOverlay(task.path, notebooks) if notebooks else None
