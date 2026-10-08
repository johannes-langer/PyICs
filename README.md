# PyICs

_Python Interactive Courses_

## What is PyICs?

PyICs is a framework for interactive Python tutorials, inspired by the _rustlings_ tutorial for the _Rust_ programming language. While PyICs comes with a basic course, it is designed to allow for easy extension with custom courses to address specific topics.

## Using the PyICs App

_The ``PyICs`` app is currently still in development an not ready to be used._

### Platform Support

PyICs runs in the terminal on Linux, macOS and Windows 10 or later. On Windows, [Windows Terminal](https://aka.ms/terminal) is recommended.

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

When editing, confirming every prompt leaves the task or course as it was. Changes are carried over to whatever depends on them:

* Tests of an exercise file that is replaced test the new file instead, and tests of a removed exercise file have to be edited or removed.
* The contents of exercise and solution files are stored in the task file. If a file on disk differs from its stored contents, you choose which to keep, seeing the difference.
* If a task is renamed, so is its entry in the course manifests below the working directory that include it.
* The progress stored in a course follows its tasks when they are reordered or removed.


The main app starts with a course selector, listing the course manifests found in the working directory and up to two directories below it (``.venv``, ``.git`` and the like are skipped). <kbd>c</kbd> goes back to it from a course, and <kbd>esc</kbd> from there returns to the course that was left. Courses left this way resume where they were left. Progress isn't stored in the course manifest: when a course is opened, the progress is checked from the exercise files on disk, task by task, and the course starts on the first unsolved task. Existing exercise files are never overwritten.

In the top level directory, there is the ``search_course.pyicscourse.json``, which is a demo course that _should_ be found by the course selector. Once selected, one of the two tests of its first task should be failing.
### Repository/App Structure

The ``src/pyics`` directory contains the following submodules:

* ``app``, which handles the ``PyIC`` controller class, the ``CLI``, key listener, actions, and user-prompts.
* ``data`` which models ``Task``s and ``Course``s
* ``scripts`` which contains the executable scripts
* ``tests`` which contains anything related to dynamic test objects (the core testing logic for student code)
* ``util`` containing logic for json handling and logging