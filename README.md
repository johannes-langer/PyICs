# PyICs

_Python Interactive Courses_

## What is PyICs?

PyICs is a framework for interactive Python tutorials, inspired by the _rustlings_ tutorial for the _Rust_ programming language. It strives to strike a balance between ease of use and feature volume, for both instructors and students.

## Installation

PyICs is available via ``pip``. Install the latest version using ``pip install pyics``, requiring ``python>=3.14``. PyICs is available on Windows (Windows 10 or later), MacOS, and Linux.

> This is a draft of the final README. PyICs has not yet been published on pypi.

## Using the PyICs App

### As a Student

#### Accessing Courses

Courses come from your instructor: either as files (a course manifest, ``*.pyicscourse.json``, and its task files) or as a course archive (a ``.zip`` file), or from a course library (a _source_) that you add once by the URL of its index, which your instructor gives you:

```sh
pyics [options] add_source [--name NAME] https://example.org/pyics-courses/index.json
pyics search                    # the courses of your sources; pyics search graphs finds matching ones
pyics pull search-algorithms    # pulls it into ./search-algorithms
pyics                           # start it from the course selector
```

- ``pyics pull`` can also take the URL or path of a course archive, and ``<source>/<course>`` if everal of your sources have a course of that name. ``--dest <directory>`` pulls it elsewhere.
- ``pyics update`` (in the course's directory, or ``pyics update <directory>``) updates a pulled course to its latest version. Your exercise files are kept as they are, and so is your progress.
- ``pyics sources`` lists your sources, and ``pyics remove_source <name>`` removes one. Sources are stored in ``sources.json`` in your pyics configuration directory (e.g. ``~/.config/pyics`` on Linux, ``~/Library/Application Support/pyics`` on macOS, ``%APPDATA%\pyics`` on Windows), so they stay when pyics is upgraded. Administrators can add sources for all users of a computer in the site configuration directory (e.g. ``/etc/xdg/pyics/sources.json``). The ``PYICS_CONFIG_DIR`` environment variable sets another configuration directory, whose sources are then the only ones used.
- The course selector also offers the courses of your sources that you haven't pulled yet, as they were when you last added, searched or pulled from them. Selecting one pulls it into the working directory.

> Courses run code on your computer, as their tests run your code together with theirs: only add sources and pull courses from people you trust. Sources are fetched over ``https`` (or from local files, e.g. on a network share); plain ``http`` has to be allowed with ``--allow_http``.

Some courses need packages besides ``pyics``, e.g. ``numpy``. They have to be installed in the same environment where PyICs is installed, siunce the tests run in the same Python interpreter. ``pyics search`` and the course selector show which of them are missing, and ``pyics pull`` prints the command that installs them (or runs it with ``--install_deps``, after asking). Packages are never installed without asking.

#### Navigating Courses

The main app starts with a course selector, listing the course manifests found in the working directory and up to two directories below it (``.venv``, ``.git`` and the like are skipped). Once you have selected a course, <kbd>c</kbd> goes back to the course selector, and <kbd>esc</kbd> from there returns to the course that was left. Courses left this way resume where they were left.

When a course is opened, it opens to the first unsolved task in the course. The main body of the app shows a number of tests which fail for the exercise file(s) for the current task, and a link to the file(s) at the very top.  
Once all tests configured for a task pass, the task counts as completed. When that is the case <kbd>n</kbd> progresses to the next task, and <kbd>p</kbd> returns to the previous one.

##### Help Display

Tasks can have hints (``help`` in the task file), which ``create_task`` and ``edit_task`` ask for after the instructions. In the main app, <kbd>h</kbd> reveals the current task's next hint above its test results, one at a time  and in order. Revealed hints stay revealed until the app is quit, also when you go to another task and back.

##### Tracing Failed Tests

When a test fails, <kbd>t</kbd> runs the task's tests again with [snoop](https://github.com/alexmojaki/snoop), which shows below each failed test what your code did: every line that was run, the values of the variables it changed, and the calls it made, with their arguments and return values. This trace mode stays on while you keep saving your exercise file, until <kbd>t</kbd> is pressed again, and it is off again for every other task you move to.

Function tests (``SimpleTest``) trace the call of the tested function, and output tests (``PrintTest``) the run of the whole file. Their authors can turn tracing off for a test by creating it with ``traceable=False``, e.g. so that its arguments aren't revealed. Tests that check the structure or the signatures of your code (``StructureTest``, ``SpecTest``) aren't traced.

### As an Instructor

PyICs packages a number of scripts to create, edit, and distribute courses. The process of creating a course and recommended practices are described below.

#### Courses and Tasks

In its background PyICs distinguishes courses and tasks. A course packages multiple tasks, and is detected by PyICs in the current working directory (recursively, up to 3 layers deep) via its file extension (``.pyicscourse.json``). A course does **not** store any information on its tasks other than paths to the course files. This will most likely be addressed in a future version.

Tasks on the other hand package information on a single task, including the paths and contents of exercise and solution files, as well as serialized versions of the configured dynamic tests (see below).

You can create a _course_ by running the following command:

```sh
pyics create_course [--fp file_path]
```

The script will ask you to either add tasks you have already created, or directly launch the task creation script which is also available as a standalone using the following command:

```sh
pyics create_task [--fp file_path]
```

Note that while the task name is mostly for indexing purposes, the task instructions are displayed in the PyICs header while the task is active. Both task and course creation scripts run interactively, but the task creation script requires you to provide **at least one exercise file**. The script does not create the file for you; it expects a path to an existing file. Exercise files are created on the student's machine when a task is opened.

Similarly, solution files are provided as paths to existing files, but a task can be created without a solution file.

Finally, test creation is run interactively (see below for details on available tests). It is recommended to have a file ready which contains Python objects required for the tests, i.e. the parameters of the tests' constructors. If such a file is provided, the script allows you to select symbols for each of the parameters from those available in the provided file. This is particularly helpful for ``SpecTest``s, as the signatures can be inferred from class or function definitions in a file.  
**Note:** If a symbol name matches a parameter name in the test's constructor, the symbol is automatically selected.

Existing tasks and courses can be edited using the following commands

```sh
pyics edit_task path-to-task-file
pyics edit_course path-to-course-file
```

#### Tests

The following tests can be configured. If a desired test can't be represented by the options below, please open an issue describing the desired behavior. Any test requires the following two parameters:

- ``name``: The name of the test, displayed in the test results if the test fails.
- ``file_path``: The path to the exercise file the test targets.

##### ``SimpleTest``

Simple tests are unit tests run on student's code. A ``SimpleTest`` additionally expects the following parameters:

- ``function_name``: The name of the function the test executes.
- ``expected``: The expected result, i.e. output of the function wrt...
- ``function_args``: Positional arguments to pass to the function in order to produce the expected output
- ``function_kwargs``: Keyword arguments to pass to the function in order to produce the expected output
- ``traceable``: Whether a detailed trace of the function can be displayed. Defaults to ``True``, can be turned off if the trace would give away too much of the solution (use sparingly).

##### ``SpecTest``

Spec tests are signature checks on the student's code. A ``SpecTest`` additionally expects the following parameters:

- ``symbol_name``: The name of the symbol where the expected signature needs to be located.
- ``expected``: The expected signature. Pass a Type (class) or function/method here. It will be converted to a spec_dict, which can then be serialized and checked against.
- ``by_type``: If this flag is set to true, check the signature based on type annotations instead of parameter/attribute names. This excludes the ``symbol_name``, which still needs to match.

##### ``StructureTest``

Structure tests can test _how_ tasks are solved by leveraging the ``ast`` builtin library. A ``StructureTest`` additionally expects the following parameter:

- ``rules``: A list of rules enforced by this test. A detailed documentation on how these rules can be configured is found in the [``StructureTest`` documentation](./structure_test.md).

##### ``PrintTest``

Print tests can test whether the stdout of students' files match a desired output. A ``PrintTest`` additionally expects the following parameters:

- ``expected``: The expected output of the targeted file to stdout.
- ``inputs``: A list of inputs to provide to the file in case the running script expects inputs (from the builtin ``input()`` function).
- ``traceable``: Whether a detailed trace of teh function can be displayed. Defaults to ``True``, can be turned off if the trace would give away too much of the solution (use sparingly).

#### Distribution

A course is distributed as an archive holding the course manifest and its task files, but not the exercise or solution files, which are created from the data stored in the task files. Progress is not tracked via the course files, but rather via the exercises that are already solved (exercise files are not overwritten if they already exist). The following command automatically creates a course archive:

```sh
pyics [options] pack [--out OUT] [--version VERSION] [--description DESCRIPTION] [--slug SLUG] [--tags TAGS [TAGS ...]] [--include INCLUDE [INCLUDE ...]] [--no_solutions] [--yes] manifest
```

The ``--include`` option can be used to add other files the course needs (e.g. datasets), and ``--no_solutions`` removes the solution files from the tasks (if they existed). ``--version``, ``--description``, and ``--tags`` are stored in the manifest, so if a course is ever re-packed it will re-use the given values. ``--slug`` is the name a course is pulled by, and it is derived from the course's name unless an explicit name is provided via this option. ``--yes`` automatically accepts confirmation prompts.  
``pack`` also finds the packages the course uses besides ``pyics`` and the standard library (imported by its exercise and solution code, or needed for the values its tests store, like numpy arrays), and offers to declare those that aren't declared in the manifest's ``requires`` yet. A task file may only make PyICs import modules of the packages its course declares: any other module is refused before the task is loaded.

``build`` indes lists the newest version of each course in the archives below the directory in ``index.json``.

```sh
pyics [options] build_index [--out OUT] [--name NAME] [directory]
```

Students add its URL with ``pyics add_source``. A course archive can also be pulled directly by its URL or path. An index looks like this, with the archives' URLs relative to it.

```json
{
    "pyics_index": 1,
    "name": "Uni Bamberg AI courses",
    "courses": {
        "search-algorithms": {
            "title": "Search Algorithms",
            "description": "DFS and BFS",
            "version": "1.0.0",
            "requires_pyics": ">=0.0.1",
            "requires": ["numpy>=1.26"],
            "url": "search-algorithms-1.0.0.zip",
            "sha256": "...",
            "tags": ["graphs"]
        }
    }
}
```

Archives are checked before anything of them is used: they can't write files outsinde the directory they are pulled to, their has h has to match the index, and they can only name the modules of the packages they declare.

### Jupyter Notebooks

Exercise files can be Jupyter notebooks (``.ipynb``) as well as Python files. Students work on them in JupyterLab, Notebook 7 or VS Code, and run PyICs next to them, e.g. in JupyterLab's or VS Code's terminal.

#### As a Student

- The tests of a task run whenever you **save** its notebook (<kbd>ctrl</kbd>+<kbd>s</kbd>, or when Jupyter autosaves it), like when you save a Python file.
- They run the code cells of the saved notebook from the top, the way _Restart Kernel and Run All Cells_ does. An error in a cell doesn't stop the run, so an unfinished exercise further down doesn't fail the tests of the exercises above it. When a test fails, its message names the cells that raised an error, e.g. ``cell 7 raised NameError: name 'qeue' is not defined``. Cells are counted from the top, markdown cells included.
- Notebook-only syntax in a test run:
  - ``%%time``, ``%%timeit`` and ``%%capture`` run their cell once, without timing it, and so do ``%time`` and ``%timeit`` for their line. What ``%%capture`` captures isn't part of the output.
  - Other cell magics (e.g. ``%%writefile``, ``%%bash``) skip their cell, and line magics (e.g. ``%matplotlib inline``, ``%pip install``) and shell commands (``!ls``) do nothing: ``files = !ls`` gives an empty list.
  - ``display(x)`` prints ``x`` as Jupyter shows it as text (its ``repr``). Plots are drawn without a window, so ``plt.show()`` doesn't wait for one to be closed.
- Cells tagged ``skip-execution`` don't run in tests, e.g. a long download or training run (tags are edited in JupyterLab's property inspector, or with _Add Cell Tag_ in VS Code).
- Output tests (``PrintTest``) compare what all cells output: what they print, and what Jupyter shows as text, i.e. displayed objects, and the value of a cell whose last line is an expression (``Out[n]``), unless it is ``None`` or the line ends with ``;``.
- Traces (<kbd>t</kbd>) and the messages of structure tests name cells and their lines, e.g. ``bfs.ipynb:cell 3`` or ``cell 3, line 2``.

#### As an Instructor

- ``create_task`` and ``edit_task`` accept notebooks as exercise and solution files, and all tests work with them. ``SimpleTest``s and ``SpecTest``s find functions and classes by name, in whichever cell students define them. ``StructureTest``s check the code of all code cells: cells that can't be parsed are left out, and named in the message if the test fails. ``PrintTest``s compare the output of all cells. Both can be restricted to a part of the notebook instead (see [Sections](#sections)).
- Notebooks are stored normalized in task files: without their outputs and execution counts, so that the outputs of solutions don't reach students and task files stay small, and with the "Python (pyics)" kernel as their kernel. A notebook you merely ran doesn't count as changed when you edit the task.
- Tag cells that tests shouldn't run (downloads, training) with ``skip-execution``.
- The tests run in the directory PyICs was started in, while Jupyter runs a notebook in the notebook's directory: a file that a notebook opens by a relative path is looked up in different directories by the two.
- ``pack`` finds the packages imported in the code cells of notebooks, too.
- An alternative that needs no notebook support is to pair a notebook with a Python file using [Jupytext](https://jupytext.readthedocs.io), and to use the ``.py`` file as the exercise file (magics are commented out in it). Students then need Jupytext in their Jupyter, and pairing tends to confuse beginners.

#### Sections

Sections link a task to a part of a notebook, e.g. to one exercise of a lecture's notebook that holds several. Students see the task's section next to the notebook's path in the title of the main panel, e.g. ``.exercises/lecture.ipynb › Exercise 2: Breadth-First Search``.

- **Where a section starts:** at a cell tagged ``pyics:<name>`` (in JupyterLab's property inspector, or with _Add Cell Tag_ in VS Code), or at a cell whose first line is the marker ``<!-- pyics: <name> -->`` (a markdown cell) or ``# pyics: <name>`` (a code cell). Names consist of letters, digits, ``_``, ``.`` and ``-``, e.g. ``ex2``. Tags are Jupyter's own way to mark cells, but hidden in most frontends. Markers are visible everywhere, and students can restore one they deleted.
- **Where it ends:** just before the next section starts, or at the end of the notebook. Cells above the first section belong to none. If several cells start a section of the same name (e.g. a copied cell), the first of them does.
- **Where to put it:** on the exercise's description, a markdown cell that students have no reason to change. The cells they add below it, split or move belong to the section, as long as they stay above the next one.
- **A task's section:** ``create_task`` and ``edit_task`` ask which section of each notebook exercise file the task is about (if the notebook has sections), and store it in the task file (``"sections"``, by the notebook's path relative to the task file). Older versions of PyICs ignore it.
- **Its tests:** ``StructureTest``s check only the code cells of the section, and fail if one of them can't be parsed. ``PrintTest``s compare only the output of the section's cells, while the whole notebook still runs, as a section usually needs the imports and data above it. ``SimpleTest``s and ``SpecTest``s find functions and classes by name, wherever they are defined, and aren't affected by sections.
- **A test's own section:** ``StructureTest``s and ``PrintTest``s use the task's section, unless they are given one of their own: ``section="ex1"`` for another section, or ``section=None`` for the whole notebook. ``create_task`` offers _The task's section_, _Whole notebook_ and the notebook's other sections. A test that uses the task's section stores nothing, so it follows when the task's section is changed.
- **A missing section fails its tests** with an explanation, e.g. ``Section 'ex2' not found: the cell that starts it (tagged 'pyics:ex2', or starting with the marker '# pyics: ex2') was removed or its tag changed.``, and the title shows ``section 'ex2' not found``. Tests never check another part of the notebook instead.
- **Several tasks in one notebook:** each task lists the same notebook among its exercise files, with its own section, and is solved on its own. The first of them that is set up creates the notebook (an existing exercise file is never overwritten), so they should all store the same contents for it: ``pack`` warns if they don't, and so does ``edit_task`` after writing a task (choose _Update_ for the notebook to store it as it is on disk).
- **The title:** the section's title is the first heading of the cell that starts it, or else its name. It is looked up in the saved notebook each time the title is drawn, so it follows the section when its cells are moved. Running PyICs in a terminal other than VS Code's, while a JupyterLab serves the notebook, makes the title a link that opens the notebook in JupyterLab, scrolled to the section. JupyterLab asks before it opens the link, in a new tab: close the notebook's old tab, or install [``jupyter-collaboration``](https://github.com/jupyterlab/jupyter-collaboration), so the two don't overwrite each other's changes. JupyterLab only opens notebooks in hidden directories (e.g. ``.exercises``) if it runs with ``--ContentsManager.allow_hidden=True``.

#### Live Results

With the "Python (pyics)" kernel, running a cell reruns the tests on what the kernel just defined, so you don't have to save after every cell. The results show up in PyICs, never in the notebook. The example course in ``examples/search_notebook`` is set up for it.

- **Setting it up:** run ``pyics install_kernel`` once. It installs the "Python (pyics)" kernel for your user, or with ``--sys_prefix`` into the current environment (where only a Jupyter installed in it finds it). The kernel runs the Python PyICs runs in, with the same packages, and loads the pyics extension at every start, restarts included. Exercise notebooks name this kernel, so JupyterLab and VS Code pick it on their own (otherwise choose it in the kernel picker). PyICs' header reminds you if the kernel isn't installed. In any other IPython kernel, ``%load_ext pyics`` loads the extension, again after every restart.
- **Running PyICs:** next to the notebook, e.g. in JupyterLab's terminal (_File › New › Terminal_) or VS Code's. The kernel finds the notebook's tasks through the course manifests in the notebook's directory and the three directories above it. If it finds none, it prints a single line in the notebook, and ``%pyics task <task file>`` names the task. ``%pyics off`` and ``%pyics on`` pause and resume live runs.
- **What runs live:** ``SimpleTest``s and ``SpecTest``s test the functions and classes as the kernel defines them right now, and ``StructureTest``s check the cells as they were last run. ``PrintTest``s only run when the notebook is saved, and keep their result from the last save. What the tested code prints doesn't show up under your cell. Interrupt the kernel (■) to stop a test stuck in an endless loop: it is reported as interrupted.
- **What PyICs shows:** each test's newest result, from the kernel or from the saved notebook. Live failures are labelled ``(live)``, and live results are underlined in the summary. The line below the summary says where the results come from:
  - _Live results from your notebook kernel (cell 5, 12:03:41). Save the notebook to count your progress._: the cell you ran last, and when.
  - _No notebook kernel connected: results are from your saved notebook._: no kernel runs the extension, or the kernel stopped responding for 10 s, e.g. after a crash (the line then says so).
  - _Your notebook kernel restarted: run your cells to see live results._
  - _Ignoring the notebook kernel …_: the kernel runs another version of pyics. Choose the "Python (pyics)" kernel and restart it.
  - _… passes in your kernel, but not in the saved notebook: was a cell deleted or not saved?_: the kernel still holds something the saved notebook doesn't, e.g. a function from a deleted cell. _Restart Kernel and Run All Cells_ shows what the saved notebook does.
- **Progress only counts the saved notebook,** as live results may depend on cells that were deleted, changed or never saved. A task solved only in the kernel shows _Solved in your notebook — save it to count it._ Saving (or autosaving) reruns the tests on the saved notebook, and their results replace the live ones. Restarting PyICs or the kernel never changes your progress, and a restarted PyICs shows the live results of a kernel that is still running right away.
- **Traces:** <kbd>t</kbd> traces the failed tests of the next live runs, too.
- **The channel:** the kernel and PyICs pass results through files in ``.pyics/`` next to the notebook, a hidden directory that git ignores (it holds its own ``.gitignore``). Both need to see the same files: a kernel on another machine is only supported if PyICs runs there too, e.g. in JupyterHub's or VS Code Remote's terminal.
- **Known limitations:** output that compiled code writes straight to the process (e.g. a C library's ``printf``), and what threads started by the tested code print, still show up under the cell. ``input()`` raises ``EOFError`` in live runs.
- **For instructors:** keep the exercise notebooks of a course in a visible directory, as ``examples/search_notebook`` does, rather than in ``.exercises/``: JupyterLab only opens notebooks in hidden directories if it runs with ``--ContentsManager.allow_hidden=True``.

## Technical Information

This project is managed using ``uv``. If you want to contribute to the project, run ``uv sync`` after cloning the repository at the top level to create a virtual environment with the required packages installed.

### Repository/App Structure

The ``src/pyics`` directory contains the following submodules:

- ``data`` which models ``Task``s and ``Course``s
- ``scripts`` which contains the executable scripts
- ``tests`` which contains anything related to dynamic test objects (the core testing logic for student code)
- ``distribution`` which packs, publishes and pulls courses, and checks the packages they need
- ``notebook`` which reads Jupyter notebooks, runs their code cells as exercise files, and finds their sections
- ``util`` containing logic for json handling and logging
