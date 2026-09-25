# PyICs

_Python Interactive Courses_

## What is PyICs?

PyICs is a framework for interactive Python tutorials, inspired by the _rustlings_ tutorial for the _Rust_ programming language. While PyICs comes with a basic course, it is designed to allow for easy extension with custom courses to address specific topics.

## Using the PyICs App

_The ``PyICs`` app is currently still in development an not ready to be used._

## Development Notes

> This project is managed using ``uv``. After cloning the repository, run ``uv sync`` at the top level to create a virtual environment with the required packages installed.

### Scripts

There are currently three scripts configured which consitute the main functionalities of the app. While tests are configured for nearly all features of the app, you may want to run the scripts for testing, especially the main ``pyics`` script.

The available scripts are the following:

1. ``uv run pyics`` (the main app)
2. ``uv run create_task`` (create a single task)
3. ``uv run create_course`` (create a course, potentially calling create_task to create new tasks instead of selecting existing ones)


In the top level directory, there is the ``search_course.pyicscourse.json``, which is a demo course that _should_ be found. When running the main script, it is currently automatically selected one of the two tests should be failing.
### Repository/App Structure

The ``src/pyics`` directory contains the following submodules:

* ``app``, which handles the ``PyIC`` controller class, the ``CLI``, key listener, actions, and user-prompts.
* ``data`` which models ``Task``s and ``Course``s
* ``scripts`` which contains the executable scripts
* ``tests`` which contains anything related to dynamic test objects (the core testing logic for student code)
* ``util`` containing logic for json handling and logging