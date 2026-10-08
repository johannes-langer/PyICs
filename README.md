# PyICs

_Python Interactive Courses_

## What is PyICs?

PyICs is a framework for interactive Python tutorials, inspired by the _rustlings_ tutorial for the _Rust_ programming language. While PyICs comes with a basic course, it is designed to allow for easy extension with custom courses to address specific topics.

## Using the PyICs App

_The ``PyICs`` app is currently still in development an not ready to be used._

### Platform Support

PyICs runs in the terminal on Linux, macOS and Windows 10 or later. On Windows, [Windows Terminal](https://aka.ms/terminal) is recommended.

### Tracing Failed Tests

When a test fails, <kbd>t</kbd> runs the task's tests again with [snoop](https://github.com/alexmojaki/snoop), which shows below each failed test what your code did: every line that was run, the values of the variables it changed, and the calls it made, with their arguments and return values. This trace mode stays on while you keep saving your exercise file, until <kbd>t</kbd> is pressed again, and it is off again for every other task you move to.

Function tests (``SimpleTest``) trace the call of the tested function, and output tests (``PrintTest``) the run of the whole file. Their authors can turn tracing off for a test by creating it with ``traceable=False``, e.g. so that its arguments aren't revealed. Tests that check the structure or the signatures of your code (``StructureTest``, ``SpecTest``) aren't traced.

### Getting Courses

Courses come from your instructor: either as files (a course manifest, ``*.pyicscourse.json``, and its task files) or as a course archive (a ``.zip`` file), or from a course library (a _source_) that you add once by the URL of its index, which your instructor gives you:

```sh
pyics add_source https://example.org/pyics-courses/index.json
pyics search                    # the courses of your sources; pyics search graphs finds matching ones
pyics pull search-algorithms    # pulls it into ./search-algorithms
pyics                           # start it from the course selector
```

* ``pyics pull`` also takes the URL or path of a course archive, and ``<source>/<course>`` if several of your sources have a course of that name. ``--dest <directory>`` pulls it elsewhere.
* ``pyics update`` (in the course's directory, or ``pyics update <directory>``) updates a pulled course to its latest version. Your exercise files are kept as they are, and so is your progress.
* ``pyics sources`` lists your sources, and ``pyics remove_source <name>`` removes one. Sources are stored in ``sources.json`` in your pyics configuration directory (e.g. ``~/.config/pyics`` on Linux, ``~/Library/Application Support/pyics`` on macOS, ``%APPDATA%\pyics`` on Windows), so they stay when pyics is upgraded. Administrators can add sources for all users of a computer in the site configuration directory (e.g. ``/etc/xdg/pyics/sources.json``). The ``PYICS_CONFIG_DIR`` environment variable sets another configuration directory, whose sources are then the only ones used.
* The course selector also offers the courses of your sources that you haven't pulled yet, as they were when you last added, searched or pulled from them. Selecting one pulls it into the working directory.

Courses run code on your computer, as their tests run your code together with theirs: only add sources and pull courses from people you trust. Sources are fetched over ``https`` (or from local files, e.g. on a network share); plain ``http`` has to be allowed with ``--allow_http``.

Some courses need packages besides pyics, e.g. ``numpy``. They have to be installed where pyics is installed, since the tests run in the same Python. ``pyics search`` and the course selector show which of them are missing, and ``pyics pull`` prints the command that installs them (or runs it with ``--install_deps``, after asking). Packages are never installed without asking.

### Distributing Courses

To publish a course, pack it into an archive, and list the archives in an index on any web server:

```sh
pyics pack search_course.pyicscourse.json --version 1.0.0 --description "DFS and BFS" --tags graphs --out library/
pyics build_index library/ --name "Uni Bamberg AI courses"
# upload library/ (index.json and the archives) to a web server
```

* An archive holds the course manifest and its task files, but neither the progress made on the course, nor the exercise and solution files, which are created from the task files when the course is started. ``--include <files or directories>`` adds other files the course needs, e.g. data its exercises read, and ``--no_solutions`` leaves the solutions out, e.g. for a course that is graded.
* ``pack`` stores the given ``--version``, ``--description`` and ``--tags`` in the manifest, so the next pack reuses them, and fixes the course's _slug_ (the name it is pulled by, derived from its name unless given with ``--slug``) and the pyics version it needs (``requires_pyics``). Give each version you publish a new version, so that pulled courses can be updated.
* ``pack`` also finds the packages the course uses besides pyics and the standard library (imported by its exercise and solution code, or needed for the values its tests store, like numpy arrays), and offers to declare those that aren't declared in the manifest's ``requires`` yet. A task file may only make pyics import modules of the packages its course declares: any other module is refused before the task is loaded.
* ``build_index`` lists the newest version of each course in the archives below the directory, in ``index.json``. Students add its URL with ``pyics add_source``. A course archive can also be pulled directly by its URL or path.

An index looks like this, with the archives' URLs relative to it:

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

Archives are checked before anything of them is used: they can't write files outside the directory they are pulled to, their hash has to match the index, and they can only name the modules of the packages they declare.

## Development Notes

> This project is managed using ``uv``. After cloning the repository, run ``uv sync`` at the top level to create a virtual environment with the required packages installed.

### Scripts

There is currently one script configured, ``pyics``, whose subcommands constitute the main functionalities of the app. While tests are configured for nearly all features of the app, you may want to run the commands for testing, especially the main app.

The available commands are the following:

1. ``uv run pyics`` (the main app)
2. ``uv run pyics create_task`` (create a single task)
3. ``uv run pyics create_course`` (create a course, potentially calling create_task to create new tasks instead of selecting existing ones)
4. ``uv run pyics edit_task <task_file>`` (edit a task, going through create_task with everything prefilled)
5. ``uv run pyics edit_course <course_file>`` (edit a course, going through create_course with everything prefilled; its tasks can be kept, edited, or removed)
6. ``uv run pyics add_source <url>``, ``remove_source <name>``, ``sources``, ``search [term]``, ``pull <course>`` and ``update [directory]`` (get courses, see [Getting Courses](#getting-courses))
7. ``uv run pyics pack <course_file>`` and ``build_index [directory]`` (distribute courses, see [Distributing Courses](#distributing-courses))

``pyics <command> --help`` lists the options of a command.

When editing, confirming every prompt leaves the task or course as it was. Changes are carried over to whatever depends on them:

* Tests of an exercise file that is replaced test the new file instead, and tests of a removed exercise file have to be edited or removed.
* The contents of exercise and solution files are stored in the task file. If a file on disk differs from its stored contents, you choose which to keep, seeing the difference.
* If a task is renamed, so is its entry in the course manifests below the working directory that include it.
* The progress stored in a course follows its tasks when they are reordered or removed.

Tasks can have hints (``help`` in the task file), which ``create_task`` and ``edit_task`` ask for after the instructions. In the main app, <kbd>h</kbd> reveals the current task's next hint above its test results, one at a time and in order. Revealed hints stay revealed until the app is quit, also when you go to another task and back.


The main app starts with a course selector, listing the course manifests found in the working directory and up to two directories below it (``.venv``, ``.git`` and the like are skipped). <kbd>c</kbd> goes back to it from a course, and <kbd>esc</kbd> from there returns to the course that was left. Courses left this way resume where they were left. Progress isn't stored in the course manifest: when a course is opened, the progress is checked from the exercise files on disk, task by task, and the course starts on the first unsolved task. Existing exercise files are never overwritten.

In the top level directory, there is the ``search_course.pyicscourse.json``, which is a demo course that _should_ be found by the course selector. Once selected, one of the two tests of its first task should be failing.
### Repository/App Structure

The ``src/pyics`` directory contains the following submodules:

* ``app``, which handles the ``PyIC`` controller class, the ``CLI``, key listener, actions, and user-prompts.
* ``data`` which models ``Task``s and ``Course``s
* ``scripts`` which contains the executable scripts
* ``tests`` which contains anything related to dynamic test objects (the core testing logic for student code)
* ``distribution`` which packs, publishes and pulls courses, and checks the packages they need
* ``util`` containing logic for json handling and logging