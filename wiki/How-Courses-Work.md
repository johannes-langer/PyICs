# How Courses Work

PyICs has two kinds of files: **tasks** and **courses**. Knowing what each one stores saves you from most surprises later.

## Tasks: `*.pyicstask.json`

A task file is self-contained. It stores:

| Field | What it is |
|---|---|
| `name` | The task's name, also its name in the course |
| `instructions` | Shown in the app's header while the task is open |
| `help` | The hints, revealed one at a time with <kbd>h</kbd> |
| `ex_files`, `exercise_code` | The paths of the exercise files, and their **starter code** |
| `sol_files`, `solution_code` | The paths of the solution files, and their contents |
| `tests` | The configured tests, serialized |
| `sections` | For notebooks: which part of the notebook the task is about (see [Jupyter Notebooks](Jupyter-Notebooks)) |

Two things follow from this:

- **The contents of the files are copied into the task file** when you create or edit it. Changing `dfs.py` on disk afterwards doesn't change the task. Run `pyics edit_task` and choose _Update_ for the file to store the new version (see [Creating Tasks](Creating-Tasks#editing-a-task)).
- **Paths are stored relative to the task file.** Moving the task together with its files keeps it working.

## Courses: `*.pyicscourse.json`

A course manifest is small. It maps task names to task files, in order, and keeps the student's progress:

```json
{
    "name": "Search Algorithms",
    "tasks": {
        "Depth-First Search": {"__pathlib__": ".tasks/dfs.pyicstask.json"},
        "Breadth-First Search": {"__pathlib__": ".tasks/bfs.pyicstask.json"}
    },
    "progress": [0, 0]
}
```

Other fields you may see:

- `free_navigation`: `true` lets students move on before solving a task ([Creating Courses](Creating-Courses#free-navigation)).
- `slug`, `version`, `description`, `tags`, `requires`, `requires_pyics`: added by `pyics pack` for distribution ([Distributing Courses](Distributing-Courses)).

The app finds courses by their file extension, in the working directory and up to two directories below it (`.venv`, `.git` and the like are skipped).

## A recommended layout

The demo course in the repository uses this layout, and it works well:

```
search-algorithms/
├── search_course.pyicscourse.json   # the course manifest
├── .tasks/
│   ├── dfs.pyicstask.json           # one file per task
│   └── bfs.pyicstask.json
├── .exercises/                      # starter code; on students' machines, their work
│   ├── dfs.py
│   └── bfs.py
├── .solutions/                      # your solutions
│   ├── dfs.py
│   └── bfs.py
└── data/                            # files the exercises read (ship them with --include)
```

The dot-directories keep the course directory tidy for students: they see the manifest, and the app links them to the exercise file they need. **Notebooks are an exception**: keep exercise notebooks in a visible directory, because JupyterLab doesn't open files in hidden directories by default (see [Jupyter Notebooks](Jupyter-Notebooks)).

If you keep courses in git, consider ignoring the generated directories on the student side. The PyICs repository ignores `.exercises/` and `.solutions/`, since the task files hold their contents anyway.

## What happens on a student's machine

When a student opens a task:

1. Its **exercise files** are created from the stored starter code, **unless they already exist**. Existing exercise files hold the student's work and are never overwritten.
2. Its **solution files** are created too, and **always rewritten** from the task file.
3. The tests run on the exercise files, and again every time one of them is saved.

Progress is checked from the exercise files: a task whose saved exercise files pass all its (non-soft) tests is solved. To start a task over, a student deletes its exercise file; it is recreated on the next open.

> **Solutions reach students unless you leave them out.** Since solution files are written next to the exercises, pack graded courses with `pyics pack --no_solutions` ([Distributing Courses](Distributing-Courses#1-pack-the-course)). Even then, the task file contains your tests and their expected values, so don't treat tests as secret.

> **Careful when you run your own course in its authoring directory.** Solution files are rewritten from the task file whenever a task opens. If you edited a solution on disk and haven't stored it with `pyics edit_task` yet, opening the task in the app replaces your edit with the stored version. Test your course from a copy, or from a pulled archive (see [Creating Courses: trying it out](Creating-Courses#trying-the-course-as-a-student)).

## Where tests run

Tests run **in the same Python interpreter as PyICs**, in the directory of the exercise file. That means:

- Relative paths in student code, like `open("edges.csv")`, work as they do when students run the file themselves.
- Packages your course uses (e.g. `numpy`) must be installed in the environment where PyICs runs. Declare them in the course so that students are told (see [Distributing Courses: dependencies](Distributing-Courses#packages-your-course-needs)).
- An exercise file can import modules next to it.

**Next:** [Creating Tasks](Creating-Tasks).
