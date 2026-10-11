# Search Algorithms (Notebook)

The search algorithms course of this repository (``search_course.pyicscourse.json``) as a single notebook: the same two tasks, Depth-First Search and Breadth-First Search, with the same instructions, hints and tests, but both exercises live in one notebook, ``search_algorithms.ipynb``, one section per task.

## Trying it

```sh
pyics install_kernel        # once: the "Python (pyics)" kernel, for live results
cd examples/search_notebook
pyics                       # select "Search Algorithms (Notebook)"
```

Starting the course creates the notebook ``search_algorithms.ipynb`` next to the course manifest (and the solution in ``.solutions/``). Open it in JupyterLab, Notebook 7 or VS Code, ideally with the PyICs app in the editor's terminal next to it. Saving the notebook reruns the tests of the current task; with the pyics kernel, running a cell shows live results in the app, too. The app's title shows the notebook's path followed by the task's section, e.g. ``search_algorithms.ipynb › Exercise 1: Depth-First Search``.

The notebook is kept outside the usual ``.exercises`` directory, since Jupyter servers don't open files in hidden directories unless they run with ``--ContentsManager.allow_hidden=True``.

## How it is built

- Both task files (``.tasks/dfs.pyicstask.json``, ``.tasks/bfs.pyicstask.json``) list the same notebook in ``ex_files`` and store the same contents for it, so the first task that is set up creates it, and each task is solved on its own.
- Each task names its section in ``sections``: ``dfs`` and ``bfs``. The sections start at the exercises' description cells, which are tagged ``pyics:dfs`` and ``pyics:bfs`` and start with the markers ``<!-- pyics: dfs -->`` and ``<!-- pyics: bfs -->``. Cells students add below a description belong to its exercise.
- The tests are those of the ``.py`` course. The function and signature tests find ``depth_first_search`` and ``breadth_first_search`` wherever they are defined. The no-recursion tests inherit their task's section, so recursion in one exercise doesn't fail the other.
