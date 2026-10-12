# pyics-jupyter

_Live results for PyICs in Jupyter notebooks._

PyICs runs the tests of a task whenever you save its exercise file. With this plugin, the tests of an exercise notebook's tasks also run in the notebook's kernel whenever you **run a cell**, and PyICs shows their results right away, without saving. Your progress still only counts the saved notebook.

## Installation

Install it into the environment PyICs runs in, and install the "Python (pyics)" Jupyter kernel:

```sh
pip install pyics-jupyter            # or: pip install "pyics[jupyter]"
python -m pyics_jupyter install      # --sys-prefix or --prefix DIR instead of for your user
```

The kernel runs the interpreter it was installed from, so it has the same PyICs and packages as the app. Install it again after moving or recreating the environment. ``--sys-prefix`` installs it into the environment, for a Jupyter installed there, and ``--prefix DIR`` into ``DIR/share/jupyter/kernels``.

## Usage

1. Open the exercise notebook in JupyterLab, Notebook 7 or VS Code, and choose the **"Python (pyics)"** kernel for it (_Kernel › Change Kernel…_ in JupyterLab, _Select Kernel_ in VS Code).
2. Run PyICs next to it, e.g. in JupyterLab's terminal (_File › New › Terminal_) or VS Code's.
3. Run cells. After each cell, the function and signature tests (``SimpleTest``, ``SpecTest``) run on the functions and classes as the kernel defines them, and PyICs shows their results within a second.

Any other IPython kernel whose environment has pyics-jupyter installed runs them after ``%load_ext pyics_jupyter``, until ``%unload_ext pyics_jupyter``.

Below the result summary, PyICs says where the results come from:

- _Live results from your notebook kernel (cell 3, 14:02:11). Save the notebook to count your progress._ Results of tests that failed in the kernel are labelled ``(live)``, e.g. ``FAILED ::adds (live)``.
- _No notebook kernel connected: results are from your saved notebook._ No kernel with the extension runs for the notebook, or it hasn't answered for 10 seconds (e.g. it was killed).
- _Solved in your notebook — save it to count it._ All tests pass in the kernel, but not yet in the saved notebook.

Notes:

- **Progress only counts the saved notebook.** Each test shows its newest result: saving the notebook after a cell run shows the results of the saved notebook again, until the next cell run.
- **Output and structure tests** (``PrintTest``, ``StructureTest``) run when the notebook is saved, as they need the whole saved notebook. Their saved results stay shown.
- **Silent:** nothing of the tests reaches the notebook. What tested functions print is captured, what they display is dropped, and ``input()`` raises ``EOFError``. The only output is a one-line warning if no task lists the notebook.
- **Endless loops:** interrupting the kernel (■) stops a test caught in one, which then fails. The kernel stays usable.
- **Finding the tasks:** the kernel looks for courses in the notebook's directory and up to three directories above it (and their subdirectories), for the tasks that list the notebook as an exercise file. It loads them when it starts: restart the kernel after ``pyics update``.
- **Several kernels** for the same notebook (e.g. two browser tabs): the newest results win.
- **The ``.pyics/`` directory** next to the notebook holds a small file per running kernel with its latest results, and a ``.gitignore`` that keeps it out of git. A kernel removes its file when it shuts down; files of crashed kernels are removed after an hour. It is safe to delete.

## Known Limitations

- Output that compiled code writes straight to the process's file descriptors (e.g. of C extensions), and output of threads that tested code starts, can still reach the cell's output.
- ``input()`` can't be answered in live runs.
- Structure and output tests only run when the notebook is saved.
- Widgets that tested code creates still reach the notebook, and so do the windows of GUI backends of matplotlib (e.g. after ``%matplotlib qt``).

## How It Works

- ``pyics_jupyter.extension``: the IPython extension. After each cell (IPython's ``post_run_cell`` event), it runs the tests in the kernel with ``pyics.tests.tests.LIVE_SOURCE`` set, so that they test the kernel's objects rather than the saved file.
- ``pyics_jupyter.channel``: the kernel writes its results to ``.pyics/<notebook>/kernel-<id>.json``, and keeps its modification time fresh as a heartbeat.
- ``pyics_jupyter.plugin`` and ``pyics_jupyter.overlay``: PyICs loads the plugin by its entry point (group ``pyics``, see ``pyics.plugins``), whose results overlay merges the newest live results into the task's status.
- ``pyics_jupyter.kernelspec``: the "Python (pyics)" kernel, which loads the extension with ``--ext=pyics_jupyter``.

The plugin lives in the PyICs repository as a member of its uv workspace: ``uv sync --all-packages`` installs it, and ``uv run pytest`` runs its tests (``plugins/pyics-jupyter/tests``) together with those of PyICs.
