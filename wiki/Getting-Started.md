# Getting Started

This page gets PyICs running and shows you a course from the student's side. That is the quickest way to see what you will be building.

## Install PyICs

PyICs needs **Python 3.14 or newer**. A virtual environment per course (or per semester) is recommended: your course's tests run in the same Python environment as PyICs, so any packages your course needs (e.g. `numpy`) must be installed there too.

```sh
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install python-interactive-courses
pyics --help
```

`pyics --help` lists every command. Each command has its own help too, e.g. `pyics create_task --help`. See the [Command Reference](Command-Reference) for all of them.

## Try the demo course

The PyICs repository includes a small course, _Search Algorithms_, with two tasks: depth-first search and breadth-first search.

```sh
git clone https://github.com/johannes-langer/PyICs
cd PyICs
pyics
```

`pyics` without a command opens the app. It starts with a **course selector** that lists the course manifests (`*.pyicscourse.json`) in the working directory and up to two levels below it. Choose _Search Algorithms_.

What happens next is what your students will see:

1. The course opens at its first unsolved task. PyICs creates the task's **exercise file** (here `.exercises/dfs.py`) from the starter code stored in the task. An exercise file that already exists is never overwritten, so students never lose work.
2. The header shows the task's **instructions**. The main panel shows a link to the exercise file and the tests that currently fail.
3. Open the exercise file in any editor, change it and **save**. The tests run again on every save.
4. Once every test passes, the task is solved and <kbd>n</kbd> moves on to the next one.

There is also a notebook version of the same course in `examples/search_notebook` (see [Jupyter Notebooks](Jupyter-Notebooks)).

### Keys in the app

| Key | Action |
|---|---|
| <kbd>n</kbd> | Next task (once the current one is solved, unless the course allows [free navigation](Creating-Courses#free-navigation)) |
| <kbd>p</kbd> | Previous task |
| <kbd>h</kbd> | Show the next hint of the task |
| <kbd>t</kbd> | Turn tracing of failed tests on or off: shows each line the student's code ran, with variable values and calls |
| <kbd>c</kbd> | Back to the course selector (<kbd>esc</kbd> there returns to the course) |
| <kbd>ctrl</kbd>+<kbd>c</kbd> | Quit |

Failing tests are red. Failing [soft tests](Choosing-Tests#options-every-test-has) are yellow: they are warnings and don't keep a task from counting as solved.

## What you will build

A course is made of **tasks**. For each task, you provide:

- a name and **instructions** (shown in the header),
- optional **hints**, revealed one at a time with <kbd>h</kbd>,
- one or more **exercise files** with starter code (Python files or Jupyter notebooks),
- optional **solution files**,
- **tests** that decide when the task is solved.

You create all of this with interactive commands. You don't write JSON by hand:

| Command | Does |
|---|---|
| `pyics create_task` | create a task ([Creating Tasks](Creating-Tasks)) |
| `pyics create_course` | create a course from tasks ([Creating Courses](Creating-Courses)) |
| `pyics edit_task`, `pyics edit_course` | change them later |
| `pyics pack`, `pyics build_index` | package and publish the course ([Distributing Courses](Distributing-Courses)) |

**Next:** [How Courses Work](How-Courses-Work) explains the files these commands create.
